"""
reasoning_agent.py
------------------
Natural-language prompt -> validated instruction, via Ollama.

    HMI --(hmi/user_prompt)--> this agent --(scheduler/instructions)--> scheduler
                                    |
                                    +--(hmi/feedback)--> HMI  (what was understood, or why not)

Changes vs. the first version, for a real deployment:
  * The LLM output is VALIDATED against site.json before anything is sent
    to the scheduler (known locations only, all fields present, sensible
    values). Invalid output is reported on hmi/feedback and dropped.
  * The allowed location list in the system prompt is generated from
    site.json, so the prompt and the validator can't drift apart.
  * Ollama is called on a worker thread, so a 60 s model call doesn't
    block the MQTT connection.
  * Optional human confirmation (REQUIRE_CONFIRMATION=1): the parsed
    instruction is shown on hmi/feedback and only forwarded after
    {"request_id": "...", "confirm": true} arrives on hmi/confirm.

Prompt payload on hmi/user_prompt: plain text, or {"prompt": "...", "request_id": "..."}.

Run:
    python3 reasoning_agent.py
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.request
import uuid
from typing import Any, Dict, Optional

if __name__ == "__main__":      # --test / --real on the command line (must run before config import)
    import modes
    modes.apply_cli_mode()

import config
from mqtt_common import connected_ok, guarded, make_client, start, stop
from validation import InstructionError, SiteConfig, validate_instruction

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reasoning_agent")

CONFIRM_TTL_S = 300


def build_system_prompt(site: SiteConfig) -> str:
    locations = "\n".join(f'  - "{name}": {meta.get("description", "")}'
                          for name, meta in site.locations.items())
    return f"""You are a strict factory automation assistant. Extract the job from the user's request as JSON.

For every location you MUST use one of these exact names (nothing else):
{locations}

Return ONLY a JSON object with exactly these keys:
- "product": string, the product name/code the user mentioned
- "source": location to pick the product up from
- "quality_check": location of the inspection (use "quality_station" if inspection is implied or not mentioned)
- "success_dest": location for parts that pass inspection
- "fail_dest": location for parts that fail inspection (use "waste_area" if not specified)

If the request is not a transport job, or a location cannot be matched to the list, return {{"error": "<short reason>"}}.
"""


def call_ollama(prompt_text: str, system_prompt: str) -> Dict[str, Any]:
    url = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
    data = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt_text,
        "system": system_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_S) as response:
        result = json.loads(response.read().decode("utf-8"))
    try:
        return json.loads(result.get("response", "{}"))
    except json.JSONDecodeError:
        raise ValueError(f"model returned invalid JSON: {result.get('response')!r:.200}")


class ReasoningAgent:
    def __init__(self, site: SiteConfig, llm=call_ollama, client_id: str = "reasoning_agent"):
        self.site = site
        self.system_prompt = build_system_prompt(site)
        self._llm = llm
        self._jobs: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=100)
        self._awaiting: Dict[str, Dict[str, Any]] = {}   # request_id -> {"instruction", "ts"}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._client = make_client(client_id, clean_session=False)
        self._client.on_connect = guarded(self._on_connect)
        self._client.on_message = guarded(self._on_message)
        self._worker = threading.Thread(target=self._work, name="llm-worker", daemon=True)

    # ---- MQTT ---------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not connected_ok(reason_code):
            logger.error("MQTT connect refused: %s", reason_code)
            return
        subs = [(config.topic(config.HMI_PROMPT_TOPIC), 1)]
        if config.REQUIRE_CONFIRMATION:
            subs.append((config.topic(config.HMI_CONFIRM_TOPIC), 1))
        client.subscribe(subs)
        logger.info("Listening for prompts on '%s'%s", config.topic(config.HMI_PROMPT_TOPIC),
                    " (confirmation required)" if config.REQUIRE_CONFIRMATION else "")

    def _on_message(self, client, userdata, msg):
        text = msg.payload.decode("utf-8", "replace").strip()
        if msg.topic == config.topic(config.HMI_CONFIRM_TOPIC):
            self._handle_confirm(text)
            return
        request_id = uuid.uuid4().hex[:8]
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("prompt"), str):
                text = obj["prompt"]
                request_id = str(obj.get("request_id") or request_id)[:64]
        except json.JSONDecodeError:
            pass
        if not text:
            return
        try:
            self._jobs.put_nowait({"prompt": text, "request_id": request_id})
        except queue.Full:
            self._feedback(request_id, ok=False, error="agent busy, prompt dropped", prompt=text)

    def _feedback(self, request_id: str, **fields):
        payload = {"request_id": request_id, "ts": time.time(), **fields}
        self._client.publish(config.topic(config.HMI_FEEDBACK_TOPIC), json.dumps(payload), qos=1)

    # ---- processing -----------------------------------------------------------------
    def process_prompt(self, prompt: str, request_id: str) -> Optional[Dict[str, Any]]:
        """Returns the validated instruction, or None (after sending feedback)."""
        logger.info("[%s] prompt: %r", request_id, prompt)
        try:
            raw = self._llm(prompt, self.system_prompt)
        except Exception as e:
            logger.error("[%s] LLM call failed: %s", request_id, e)
            self._feedback(request_id, ok=False, prompt=prompt, error=f"language model error: {e}")
            return None
        if isinstance(raw, dict) and "error" in raw and len(raw) == 1:
            self._feedback(request_id, ok=False, prompt=prompt, error=f"not understood: {raw['error']}")
            return None
        if isinstance(raw, dict):
            raw.pop("request_id", None)
        try:
            instruction = validate_instruction(raw, self.site)
        except InstructionError as e:
            logger.warning("[%s] LLM output rejected: %s (raw=%r)", request_id, e, raw)
            self._feedback(request_id, ok=False, prompt=prompt, error=str(e), llm_output=raw)
            return None
        instruction["request_id"] = request_id
        return instruction

    def _work(self):
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=1)
            except queue.Empty:
                self._expire_confirmations()
                continue
            instruction = self.process_prompt(job["prompt"], job["request_id"])
            if instruction is None:
                continue
            if config.REQUIRE_CONFIRMATION:
                with self._lock:
                    self._awaiting[job["request_id"]] = {"instruction": instruction, "ts": time.time()}
                self._feedback(job["request_id"], ok=True, needs_confirmation=True, instruction=instruction,
                               how_to_confirm={"topic": config.topic(config.HMI_CONFIRM_TOPIC),
                                               "payload": {"request_id": job["request_id"], "confirm": True}})
            else:
                self._forward(instruction)

    def _forward(self, instruction: Dict[str, Any]):
        self._client.publish(config.topic(config.INSTRUCTION_TOPIC), json.dumps(instruction), qos=1)
        logger.info("[%s] -> %s %s", instruction["request_id"], config.topic(config.INSTRUCTION_TOPIC), instruction)
        self._feedback(instruction["request_id"], ok=True, forwarded=True, instruction=instruction)

    def _handle_confirm(self, text: str):
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        rid = str(msg.get("request_id"))
        with self._lock:
            entry = self._awaiting.pop(rid, None)
        if entry is None:
            self._feedback(rid, ok=False, error="nothing awaiting confirmation with this request_id")
        elif msg.get("confirm") is True:
            self._forward(entry["instruction"])
        else:
            self._feedback(rid, ok=True, discarded=True)

    def _expire_confirmations(self):
        now = time.time()
        with self._lock:
            expired = [rid for rid, e in self._awaiting.items() if now - e["ts"] > CONFIRM_TTL_S]
            for rid in expired:
                del self._awaiting[rid]
        for rid in expired:
            self._feedback(rid, ok=False, error="confirmation timed out; prompt discarded")

    # ---- lifecycle -----------------------------------------------------------------------
    def run_forever(self):
        self._worker.start()
        start(self._client)
        try:
            while not self._stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            stop(self._client)


def main():
    ReasoningAgent(SiteConfig.load(config.SITE_CONFIG_PATH)).run_forever()


if __name__ == "__main__":
    main()
