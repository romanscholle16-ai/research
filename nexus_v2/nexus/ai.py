"""Optional AI providers (local/free only, configured by environment). AI is advisory text generation: it is never used
for execution simulation, accounting, qualification or risk decisions, and the system works identically without it.

Environment (all optional, never logged):
  NEXUS_OLLAMA_URL   e.g. http://127.0.0.1:11434   (local Ollama)   NEXUS_OLLAMA_MODEL
  NEXUS_AI_URL / NEXUS_AI_KEY / NEXUS_AI_MODEL    an OpenAI-compatible endpoint the user has chosen (free tier or local)
"""
import json, os, time, urllib.request


class Provider:
    name = "base"

    def generate(self, prompt: str, timeout=20) -> str:
        raise NotImplementedError


class Deterministic(Provider):
    """Always available fallback: templated text from stored facts. No network."""
    name = "deterministic"

    def generate(self, prompt, timeout=0):
        return "[deterministic summary] " + prompt[:400]


class Ollama(Provider):
    name = "ollama"

    def __init__(self, url, model):
        self.url, self.model = url.rstrip("/"), model

    def generate(self, prompt, timeout=20):
        req = urllib.request.Request(self.url + "/api/generate", json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode(),
                                     {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())["response"]


class OpenAICompat(Provider):
    name = "openai_compat"

    def __init__(self, url, key, model):
        self.url, self.key, self.model = url.rstrip("/"), key, model

    def generate(self, prompt, timeout=20):
        req = urllib.request.Request(self.url + "/chat/completions", json.dumps({"model": self.model, "messages": [{"role": "user", "content": prompt}]}).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())["choices"][0]["message"]["content"]


class Router:
    """Tries configured providers in order with a circuit breaker (exponential backoff after failures) and falls back
    to the deterministic provider. A failing provider is not called again until its backoff expires."""

    def __init__(self, providers=None, now=time.time):
        self.now = now
        if providers is None:
            providers = []
            if os.environ.get("NEXUS_OLLAMA_URL"):
                providers.append(Ollama(os.environ["NEXUS_OLLAMA_URL"], os.environ.get("NEXUS_OLLAMA_MODEL", "llama3")))
            if os.environ.get("NEXUS_AI_URL") and os.environ.get("NEXUS_AI_KEY"):
                providers.append(OpenAICompat(os.environ["NEXUS_AI_URL"], os.environ["NEXUS_AI_KEY"], os.environ.get("NEXUS_AI_MODEL", "default")))
        self.providers = providers + [Deterministic()]
        self.fail = {p.name: [0, 0.0] for p in self.providers}     # name -> [consecutive failures, retry_at]
        self.log = []

    def generate(self, prompt):
        for p in self.providers:
            n, retry_at = self.fail[p.name]
            if self.now() < retry_at:
                continue
            try:
                out = p.generate(prompt)
                self.fail[p.name] = [0, 0.0]
                return out, p.name
            except Exception as e:                       # record, back off (60s * 2^n, max 1h), try the next provider
                n += 1
                self.fail[p.name] = [n, self.now() + min(3600, 60 * 2 ** n)]
                self.log.append((self.now(), p.name, type(e).__name__))
        return Deterministic().generate(prompt), "deterministic"

    def status(self):
        return {n: {"consecutive_failures": f[0], "retry_in_s": max(0, round(f[1] - self.now()))} for n, f in self.fail.items()}
