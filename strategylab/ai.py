"""Provider-independent AI interface with availability detection, fallback, bounded retries, token/cost tracking, budgets.

Providers: anthropic (ANTHROPIC_API_KEY), openai_compat (OPENAI_API_KEY [+OPENAI_BASE_URL], OPENAI_MODEL),
ollama (local, http://localhost:11434), deterministic (always available; returns None => caller uses rule-based generation).
AI is OPTIONAL: all deterministic research continues without it. AI output is only ever a *proposal*: every spec is
validated (strategies.validate_spec) and backtested through the same pipeline; AI cannot alter results or qualification.
"""
from __future__ import annotations
import json, os, time, urllib.request, urllib.error
from dataclasses import dataclass

PRICE_PER_MTOK = {  # (in, out) USD per million tokens - approximate, configurable; used only for budgeting
    "anthropic": (3.0, 15.0), "openai_compat": (2.5, 10.0), "ollama": (0.0, 0.0)}


import re
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,}|x-api-key['\"]?\s*[:=]\s*\S+)", re.I)


def scrub(text, extra_secrets=()):
    """Redact API keys from anything that may be logged or stored."""
    t = _SECRET.sub("[REDACTED]", str(text))
    for k in list(extra_secrets) + [os.environ.get(n, "") for n in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]:
        if k and len(k) > 8: t = t.replace(k, "[REDACTED]")
    return t


class QuotaExhausted(Exception): pass
class ProviderUnavailable(Exception): pass


@dataclass
class AIResult:
    text: str; provider: str; tokens_in: int; tokens_out: int; cost: float


def _post(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="ignore")[:300]
        if e.code in (402, 429) or "quota" in txt.lower() or "credit" in txt.lower() or "billing" in txt.lower():
            raise QuotaExhausted(f"{e.code} {txt}")
        if e.code in (401, 403): raise ProviderUnavailable(f"auth {e.code}")
        raise ProviderUnavailable(f"http {e.code} {txt}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderUnavailable(str(e))


class Provider:
    name = "base"
    def available(self) -> bool: return False
    def complete(self, system: str, prompt: str, max_tokens: int) -> tuple[str, int, int]: raise NotImplementedError


class Anthropic(Provider):
    name = "anthropic"
    def __init__(self, model): self.model = model
    def available(self): return bool(os.environ.get("ANTHROPIC_API_KEY"))
    def complete(self, system, prompt, max_tokens):
        r = _post("https://api.anthropic.com/v1/messages", {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                  {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": prompt}]})
        return "".join(b.get("text", "") for b in r["content"]), r["usage"]["input_tokens"], r["usage"]["output_tokens"]


class OpenAICompat(Provider):
    name = "openai_compat"
    def available(self): return bool(os.environ.get("OPENAI_API_KEY"))
    def complete(self, system, prompt, max_tokens):
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        r = _post(base + "/chat/completions", {"authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                  {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        u = r.get("usage", {})
        return r["choices"][0]["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


class Ollama(Provider):
    name = "ollama"
    def available(self):
        try:
            urllib.request.urlopen(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/tags", timeout=1); return True
        except Exception: return False
    def complete(self, system, prompt, max_tokens):
        r = _post(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate",
                  {}, {"model": os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"), "system": system, "prompt": prompt, "stream": False,
                       "options": {"num_predict": max_tokens}}, timeout=180)
        return r.get("response", ""), r.get("prompt_eval_count", 0), r.get("eval_count", 0)


class Router:
    """Tries providers in configured order; tracks usage & budget in the DB; remembers cool-downs for failed providers."""
    def __init__(self, db, settings, providers=None):
        self.db, self.st = db, settings
        self.providers = providers if providers is not None else [Anthropic(settings.ai_model_anthropic), OpenAICompat(), Ollama()]
        self.cool = {}   # provider -> retry-at

    def _spent(self, since):
        r = self.db.q("SELECT COALESCE(SUM(cost_usd),0) c FROM ai_usage WHERE ts>=?", (since,))
        return r[0]["c"]

    def budget_ok(self):
        now = time.time()
        return self._spent(now - 86400) < self.st.ai_daily_budget_usd and self._spent(now - 30 * 86400) < self.st.ai_monthly_budget_usd

    def status(self):
        out = []
        for p in self.providers:
            out.append(dict(name=p.name, available=bool(p.available()), cooldown_s=max(0, int(self.cool.get(p.name, 0) - time.time()))))
        return dict(providers=out, budget_ok=self.budget_ok(), spent_24h=self._spent(time.time() - 86400), spent_30d=self._spent(time.time() - 30 * 86400))

    def ask(self, system, prompt, max_tokens=1500) -> AIResult | None:
        prompt = scrub(prompt)      # never send keys to a model, even by accident
        """Returns None when no AI is usable right now (caller falls back to deterministic logic). Never raises."""
        if not self.budget_ok():
            self.db.set("ai_state", "budget_exhausted"); return None
        reason = None
        for name in self.st.ai_provider_order:
            p = next((x for x in self.providers if x.name == name), None)
            if p is None or time.time() < self.cool.get(p.name, 0) or not p.available(): continue
            for attempt in range(self.st.ai_max_retries + 1):
                try:
                    text, ti, to = p.complete(system, prompt, max_tokens)
                    pin, pout = PRICE_PER_MTOK.get(p.name, (3.0, 15.0)); cost = (ti * pin + to * pout) / 1e6
                    self.db.x("INSERT INTO ai_usage(ts,provider,model,tokens_in,tokens_out,cost_usd,ok,note) VALUES(?,?,?,?,?,?,1,'')",
                              (time.time(), p.name, getattr(p, "model", ""), ti, to, cost))
                    self.db.set("ai_state", "ok:" + p.name)
                    return AIResult(text, p.name, ti, to, cost)
                except QuotaExhausted as e:
                    self.cool[p.name] = time.time() + 3600; reason = f"quota_exhausted:{p.name}"; self.db.set("ai_state", reason)
                    self.db.log("warn", "ai", scrub(f"{p.name} quota exhausted; falling back ({e})")); break
                except ProviderUnavailable as e:
                    time.sleep(min(2 ** attempt, 4) if not os.environ.get("STRATEGYLAB_FAST") else 0)
                    if attempt == self.st.ai_max_retries:
                        self.cool[p.name] = time.time() + 300; self.db.log("warn", "ai", scrub(f"{p.name} unavailable: {e}"))
                except Exception as e:  # malformed response etc.
                    self.cool[p.name] = time.time() + 300; self.db.log("warn", "ai", scrub(f"{p.name} error: {e}")); break
        self.db.set("ai_state", reason or "none_available")
        return None
