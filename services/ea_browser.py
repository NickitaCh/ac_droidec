"""EA's email/one-time-code browser flow, with isolated server-side cookies.

No passwords, captured user cookies, CAPTCHA workarounds or arbitrary URLs.
If EA requests another kind of challenge, stop and report that requirement.
"""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from services.game_account import AccountError

HOSTS = {"accounts.ea.com", "signin.ea.com"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def auth_url(config):
    now = datetime.now(timezone.utc)
    payload = {**config["sig_payload"], "ts": now.strftime("%Y-%m-%d %H:%M:%S:") + f"{now.microsecond // 1000:03d}"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.new(base64.b64decode(config["signing_key"]), encoded, hashlib.sha256).digest()).rstrip(b"=")
    params = {**config["auth_params"], "nonce": secrets.token_hex(16), "sig": (encoded + b"." + signature).decode()}
    return "https://accounts.ea.com/connect/auth?" + urlencode(params)


def _check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise AccountError("EA перенаправила вход на неподдерживаемую страницу. Подключение остановлено.")


def _cookies(client):
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in client.cookies.jar]


def _client(cookies=()):
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return httpx.AsyncClient(cookies=jar, timeout=25, follow_redirects=False,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})


async def _walk(client, url, data=None):
    for _ in range(10):
        _check_url(url)
        try:
            response = await client.post(url, data=data) if data is not None else await client.get(url)
        except httpx.HTTPError:
            raise AccountError("Не удалось связаться со страницей входа EA. Попробуйте позже.") from None
        data = None
        if response.status_code in (301, 302, 303, 307, 308):
            location = urljoin(url, response.headers.get("location", ""))
            parsed = urlsplit(location)
            if parsed.scheme == "ealogin" and parsed.netloc == "SWGOH_GP_CLIENT":
                values = {**parse_qs(parsed.query), **parse_qs(parsed.fragment)}
                token = values.get("lnglv_token", [""])[0]
                if not token:
                    raise AccountError("EA не выдала длительный доступ к аккаунту.")
                return {"stage": "complete", "longlive": token}
            url = location
            continue
        if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
            raise AccountError("EA не открыла форму входа. Попробуйте позже.")
        soup = BeautifulSoup(response.text, "html.parser")
        # The code page has a separate resend form before the actual code form.
        form = next((f for f in soup.find_all("form") if f.select_one('input[name="oneTimeCode"]')), None)
        if form is None:
            form = next((f for f in soup.find_all("form") if f.select_one('input[name="email"]')), soup.find("form"))
        if not form:
            raise AccountError("EA запросила дополнительную проверку. Автоматический вход остановлен.")
        fields = {item["name"]: item.get("value", "") for item in form.select("input[name]") if item.get("type") == "hidden"}
        names = {item.get("name") for item in form.select("input[name]")}
        if "oneTimeCode" in names:
            stage = "code"
        elif "email" in names:
            stage = "email"
        elif "emailVerifyCode" in names or "contactMe" in names:
            raise AccountError("Сначала подключите EA Connect к этому аккаунту в самой игре.")
        else:
            raise AccountError("EA запросила дополнительное подтверждение. Этот способ входа пока не поддерживается.")
        post_url = urljoin(url, form.get("action") or url)
        _check_url(post_url)
        return {"stage": stage, "url": post_url, "fields": fields, "cookies": _cookies(client)}
    raise AccountError("EA выполнила слишком много перенаправлений. Начните вход заново.")


async def start(config, email):
    if len(email) > 254 or "@" not in email or any(c.isspace() for c in email):
        raise AccountError("Введите адрес почты, к которому привязан EA Connect в игре.")
    async with _client() as client:
        page = await _walk(client, auth_url(config))
        if page["stage"] != "email":
            raise AccountError("EA не открыла форму ввода почты.")
        data = {**page["fields"], "email": email, "_eventId": "submit", "loginMethod": "emailOtc", "rememberMe": "on"}
        data.pop("password", None)
        result = await _walk(client, page["url"], data)
        if result["stage"] == "email":
            raise AccountError("EA не приняла адрес или запросила дополнительную проверку.")
        return result


async def finish(pending, code):
    code = code.strip()
    if not code.isascii() or not code.isdigit() or not 4 <= len(code) <= 10:
        raise AccountError("Введите цифровой код из письма EA.")
    async with _client(pending["cookies"]) as client:
        data = {**pending["fields"], "oneTimeCode": code, "_eventId": "submit"}
        return await _walk(client, pending["url"], data)
