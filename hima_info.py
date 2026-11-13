#!/usr/bin/env python3
"""
HIMA Info — 鸿蒙智行车辆数据工具（只读）。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import secrets
import socket
import ssl
import string
import subprocess
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
PROG = Path(__file__).name
SESSION_FILE = BASE_DIR / "session.json"
LOCK_FILE = BASE_DIR / "session.lock"
CERT_DIR = BASE_DIR / "certs"

OMP = "https://omp.uopes.cn"
USER_AUTH = f"{OMP}/xcar/omp/xbs/account/user/auth"
USER_REFRESH = f"{OMP}/xcar/omp/xbs/account/user/refresh"
USER_KICKOUT = f"{OMP}/xcar/omp/xbs/account/user/kickout"
VEHICLE_AUTH = f"{OMP}/xcar/omp/xbs/account/vehicle/auth"
VEHICLE_REFRESH = f"{OMP}/xcar/omp/xbs/account/vehicle/refresh"

OAUTH_AUTHORIZE = "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize"
CLIENT_ID = "104871165"
REDIRECT_URI = "https://xop.xcar.test.vmall.com/"
SCOPE = (
    "openid profile https://www.huawei.com/auth/account/base.profile "
    "https://www.huawei.com/auth/account/birthday https://www.huawei.com/auth/account/realname/state "
    "https://www.huawei.com/auth/account/country https://www.huawei.com/auth/account/realname/level "
    "openid https://www.huawei.com/auth/account/mobile.number email "
)
UA = "X-Car-APP-Android-3.0.2.300"

# enterpriseCode 关键词 → (车辆云地址, 品牌 CA 文件, 显示名)
BRANDS = {
    "SERES": ("https://apig.fgaiservice.com", "brand-seres.pem", "问界"),
    "CHERY": ("https://apir.chssatsp.icvcs.com", "brand-chery.pem", "智界"),
    "BJEV": ("https://apir.bjevssa.icvcs.com", "brand-bjev.pem", "享界"),
    "JAC": ("https://apir.jacssa.icvcs.com", "brand-jac.pem", "尊界"),
    "SRIH": ("https://api-app.srih.icvcs.com", "brand-saic.pem", "上汽"),
}
# apig.fgaiservice.com 只发叶子证书；品牌 CA 文件缺失时用叶子指纹 pinning。
SERES_LEAF_SHA256 = "4a8c21f89b7f13ddc45217d6ba2d7ceab326b480f1fd2512003d2697dd132282"

DYNAMIC_KEYS = (
    "airConditionPlan", "charge", "departurePlan", "door", "fuel", "hvac", "lamp",
    "location", "seat", "sentryPlan", "tire", "vehicleStatus", "warmCoolingBox",
    "welcome", "window",
)
GEARS = {0: "N", 13: "R", 14: "D", 15: "P"}


class OmpError(RuntimeError):
    def __init__(self, code, msg: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"OMP {code}: {msg[:200]}")


class NeedsLogin(RuntimeError):
    """refreshToken 失效或没有会话，只能重新 login。"""


class TokenInvalid(RuntimeError):
    """车辆 token 被服务端拒绝，需要刷新。"""


# ---------- HTTP 会话与 OMP 基础 ----------

def trace_id() -> str:
    return (f"{random.randint(0, 99999):05d}{int(time.time() * 1000) % 1000:03d}"
            + "".join(secrets.choice(string.ascii_uppercase) for _ in range(4)))


def omp_headers(session_key: str = "", user_id: str = "", model: str = "Pixel 7", ec: str = "") -> dict:
    return {
        "Accept": "*/*", "Accept-Language": "zh-cn", "Content-Type": "application/json",
        "pkgName": "app.huawei.auto", "traceID": trace_id(), "User-Agent": UA,
        "xid": session_key, "uid": user_id, "EC": ec, "deviceModel": model,
    }


_OMP_HTTP: requests.Session | None = None
_OMP_WARMED = False


def omp_session() -> requests.Session:
    global _OMP_HTTP
    if _OMP_HTTP is None:
        _OMP_HTTP = requests.Session()
    return _OMP_HTTP


def omp_warm() -> None:
    """App 先访问公开页面建立 WAF Cookie（HWWAFSESID/HWWAFSESTIME）。"""
    global _OMP_WARMED
    if _OMP_WARMED:
        return
    try:
        omp_session().post(f"{OMP}/xcar/omp/xbs/page/queryCustomizedPage",
                           json={"pageType": 0}, headers=omp_headers(), timeout=15)
    except requests.RequestException:
        pass
    _OMP_WARMED = True


def omp_payload(body) -> dict:
    if not isinstance(body, dict):
        raise OmpError(-1, "响应不是 JSON 对象")
    if body.get("code") not in (0, 200):
        raise OmpError(body.get("code"), str(body.get("msg") or body.get("resultCode") or ""))
    data = body.get("data")
    return data if isinstance(data, dict) and data else body


def status_of(payload: dict) -> str:
    """sessionKeyStatus：1=设备未受信（vehicle/auth 会 10011），0=受信。"""
    info = payload.get("userInfo") if isinstance(payload.get("userInfo"), dict) else {}
    value = info.get("sessionKeyStatus")
    if value in (None, ""):
        value = payload.get("sessionKeyStatus")
    return "" if value is None else str(value)


def normalize_expires(value) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return n // 1000 if n > 10_000_000_000 else n


# ---------- 设备身份 ----------

def machine_uuid() -> str:
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                val = line.split('"')[3]
                if val:
                    return val
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return socket.gethostname()


def derive_device() -> tuple[str, str]:
    base = os.environ.get("AITO_DEVICE_STR") or machine_uuid()
    return (hashlib.sha256(base.encode()).hexdigest().upper(),
            os.environ.get("AITO_DEVICE_MODEL", "Pixel 7"))


# ---------- OMP 认证 ----------

def omp_user_auth(code: str, device_id: str, model: str) -> dict:
    omp_warm()
    r = omp_session().post(
        USER_AUTH,
        json={"authCode": code, "clientType": "android",
              "device": {"type": "1", "id": device_id, "model": model, "adDeviceId": ""},
              "kickOut": False},  # 若返回 status=1，下面再显式 kickout
        headers=omp_headers(model=model), timeout=30,
    )
    payload = omp_payload(r.json())
    if not payload.get("sessionKey") or not isinstance(payload.get("oauthToken"), dict):
        raise RuntimeError(f"user/auth 响应缺少会话字段: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return payload


def omp_user_refresh(at: str, rt: str, session_key: str, user_id: str,
                     device_id: str, model: str, ec: str = "") -> dict:
    omp_warm()
    r = omp_session().post(
        USER_REFRESH,
        json={"at": at, "rt": rt, "clientType": "android",
              "device": {"type": "1", "id": device_id, "model": model}},
        headers=omp_headers(session_key, user_id, model, ec), timeout=30,
    )
    payload = omp_payload(r.json())
    if not payload.get("sessionKey") or not isinstance(payload.get("oauthToken"), dict):
        raise RuntimeError(f"user/refresh 响应缺少会话字段: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return payload


def omp_user_kickout(session_key: str, user_id: str, device_id: str, model: str) -> dict:
    """顶掉当前在线设备，把 sessionKeyStatus 从 1 变成 0（手机会掉线）。"""
    omp_warm()
    r = omp_session().post(
        USER_KICKOUT,
        json={"deviceInfo": {"type": "1", "id": device_id, "model": model}},
        headers=omp_headers(session_key, user_id, model, ""), timeout=30,
    )
    return omp_payload(r.json())


def trust_session(payload: dict, session_key: str, user_id: str,
                  device_id: str, model: str) -> dict:
    """status=1 时 kickout；成功后把返回的 userInfo/sessionKey 合并回 payload。"""
    if status_of(payload) != "1":
        return payload
    kicked = omp_user_kickout(session_key, user_id, device_id, model)
    new_status = status_of(kicked)
    if new_status != "0":
        raise RuntimeError(f"user/kickout 后 sessionKeyStatus={new_status or '缺失'}")
    if kicked.get("userInfo"):
        payload["userInfo"] = kicked["userInfo"]
    if kicked.get("serviceUserInfo") is not None:
        payload["serviceUserInfo"] = kicked["serviceUserInfo"]
    if kicked.get("sessionKey"):
        payload["sessionKey"] = kicked["sessionKey"]
        if kicked.get("sessionKeyExpireIn"):
            payload["sessionKeyExpireIn"] = kicked["sessionKeyExpireIn"]
    payload["sessionKeyStatus"] = "0"
    return payload


def omp_vehicle_auth(session_key: str, user_id: str, device_id: str, model: str, ec: str) -> dict:
    omp_warm()
    r = omp_session().post(
        VEHICLE_AUTH,
        json={"deviceInfo": {"type": "1", "id": device_id, "model": model}},
        headers=omp_headers(session_key, user_id, model, ec), timeout=30,
    )
    payload = omp_payload(r.json())
    if not payload.get("vehicleTokenInfoList"):
        raise RuntimeError(f"vehicle/auth EC={ec!r} 未返回车辆 token")
    return payload


def select_token(payload: dict, ec: str = "", account_id: str = "") -> dict | None:
    vlist = payload.get("vehicleTokenInfoList") or []
    if account_id:
        for t in vlist:
            if str(t.get("accountId", "")) == str(account_id):
                return t
    if ec and ec.upper() != "ALL":
        for t in vlist:
            if ec.upper() in str(t.get("enterpriseCode", "")).upper():
                return t
    if vlist:
        return vlist[0]
    return payload if payload.get("accessToken") else None


def omp_vehicle_refresh(session_key: str, user_id: str, ec: str, device_id: str,
                        model: str, account_id: str = "") -> dict:
    omp_warm()
    r = omp_session().post(
        VEHICLE_REFRESH,
        json={"tokenType": 1, "deviceInfo": {"type": "1", "id": device_id, "model": model},
              "requireAccountId": not bool(account_id)},
        headers=omp_headers(session_key, user_id, model, ec), timeout=30,
    )
    payload = omp_payload(r.json())
    entry = select_token(payload, ec, account_id)
    if not entry or not entry.get("accessToken"):
        raise RuntimeError(f"vehicle/refresh EC={ec!r} 未返回 token")
    return entry


# ---------- 会话与 token 签发 ----------

def _account_from_token(token: dict) -> dict:
    return {
        "enterpriseCode": token.get("enterpriseCode") or "SERES",
        "accountId": str(token.get("accountId") or ""),
        "vehicleAccessToken": token.get("accessToken") or "",
        "vehicleTokenExpiresAt": normalize_expires(token.get("expiresAt")),
    }


def issue_accounts(session_key: str, user_id: str, device_id: str, model: str) -> list[dict]:
    """用同一 OMP 会话签发账号下所有品牌的车辆 token。

    实测：后一次 vehicle/auth 会重绑定会话，使先签发品牌的 token 作废。
    所以对较早的分组用 vehicle/refresh 原地补签，最后一组保持不动。
    """
    groups: list[tuple[str, list[dict]]] = []
    for ec_req in ("", "ALL"):
        try:
            payload = omp_vehicle_auth(session_key, user_id, device_id, model, ec_req)
        except (OmpError, RuntimeError) as exc:
            print(f"[warn] vehicle/auth EC={ec_req!r}: {str(exc)[:120]}")
            continue
        tokens = payload.get("vehicleTokenInfoList") or []
        if tokens:
            groups.append((ec_req, tokens))

    if not groups:
        raise RuntimeError("两种身份的 vehicle/auth 均未返回车辆")

    last = len(groups) - 1
    seen: set[str] = set()
    accounts: list[dict] = []
    for group_index, (_ec_req, tokens) in enumerate(groups):
        for token in tokens:
            key = str(token.get("accountId") or token.get("enterpriseCode")
                      or token.get("accessToken", ""))
            if key in seen:
                continue
            seen.add(key)
            ec = token.get("enterpriseCode") or ("SERES" if group_index == 0 else "")
            if group_index != last and ec:
                try:
                    fresh = omp_vehicle_refresh(session_key, user_id, ec, device_id, model,
                                                str(token.get("accountId") or ""))
                    token = {**token,
                             "accessToken": fresh.get("accessToken", token.get("accessToken")),
                             "expiresAt": fresh.get("expiresAt", token.get("expiresAt"))}
                except (OmpError, RuntimeError) as exc:
                    print(f"[warn] 补签 {ec} 失败（使用时再续）: {str(exc)[:100]}")
            accounts.append(_account_from_token(token))
    return accounts


def login_with_code(code: str) -> dict:
    device_id, model = derive_device()
    session = omp_user_auth(code, device_id, model)
    session_key = session.get("sessionKey", "")
    user_id = str((session.get("userInfo") or {}).get("userId") or session.get("userId") or "")
    if not session_key or not user_id:
        raise RuntimeError("user/auth 未返回 sessionKey/userId")

    if status_of(session) == "1":
        session = trust_session(session, session_key, user_id, device_id, model)
        session_key = session.get("sessionKey", session_key)
        print("[login] sessionKeyStatus=1，已 user/kickout 顶掉在线设备（会话转受信）")

    oauth = session.get("oauthToken") or {}
    accounts = issue_accounts(session_key, user_id, device_id, model)
    return {
        "userAccessToken": oauth.get("accessToken", ""),
        "userRefreshToken": oauth.get("refreshToken", ""),
        "userId": user_id,
        "sessionKey": session_key,
        "sessionKeyExpiresAtUnix": int(time.time()) + int(session.get("sessionKeyExpireIn") or 0),
        "deviceId": device_id,
        "deviceModel": model,
        "accounts": accounts,
        "updatedAtUnix": int(time.time()),
    }


@contextmanager
def renew_lock():
    """单飞：避免并发签发互相注销（100015）。"""
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _device(state: dict) -> tuple[str, str]:
    return (state.get("deviceId") or derive_device()[0],
            state.get("deviceModel") or "Pixel 7")


def _require_refreshable(state: dict | None) -> None:
    if not state or not state.get("userRefreshToken") or not state.get("userId"):
        raise NeedsLogin(f"没有可刷新的用户 refresh token，请先运行：python3 {PROG} login")


def _roll_user_session(state: dict, kickout: bool) -> tuple[dict, bool]:
    """锁内滚动 at/rt/sessionKey；kickout=True 时把 status=1 转成受信。"""
    disk = load_state()
    if disk and (disk.get("sessionKey") != state.get("sessionKey")
                 or int(disk.get("updatedAtUnix") or 0) > int(state.get("updatedAtUnix") or 0)):
        state = disk  # 另一个进程已经刷新过

    device_id, model = _device(state)
    accounts = state.get("accounts") or []
    ec0 = accounts[0].get("enterpriseCode", "") if accounts else ""
    try:
        user = omp_user_refresh(state.get("userAccessToken", ""), state["userRefreshToken"],
                                state.get("sessionKey", ""), state["userId"], device_id, model, ec0)
    except OmpError as exc:
        if str(exc.code) == "10022":
            raise NeedsLogin(f"refreshToken 已失效（10022），请重新运行：python3 {PROG} login") from exc
        raise

    oauth = user.get("oauthToken") or {}
    state["userAccessToken"] = oauth.get("accessToken") or state.get("userAccessToken", "")
    state["userRefreshToken"] = oauth.get("refreshToken") or state["userRefreshToken"]
    state["sessionKey"] = user.get("sessionKey") or state.get("sessionKey", "")
    if user.get("sessionKeyExpireIn"):
        state["sessionKeyExpiresAtUnix"] = int(time.time()) + int(user["sessionKeyExpireIn"])

    kicked = False
    if kickout and status_of(user) == "1":
        user = trust_session(user, state["sessionKey"], state["userId"], device_id, model)
        state["sessionKey"] = user.get("sessionKey") or state["sessionKey"]
        if user.get("sessionKeyExpireIn"):
            state["sessionKeyExpiresAtUnix"] = int(time.time()) + int(user["sessionKeyExpireIn"])
        kicked = True
        print("[refresh] sessionKeyStatus=1，已 user/kickout 顶掉在线设备（会话转受信）")

    state["updatedAtUnix"] = int(time.time())
    save_state(state)
    return state, kicked


def refresh_user_only(state: dict | None = None) -> dict:
    """只滚动 at/rt/sessionKey，不签发车辆 token，也不 kickout（手机可继续在线）。"""
    state = state or load_state()
    _require_refreshable(state)
    with renew_lock():
        state, _kicked = _roll_user_session(state, kickout=False)
    return state


def refresh_state(state: dict | None = None, *, force: bool = False) -> dict:
    state = state or load_state()
    _require_refreshable(state)
    with renew_lock():
        state, kicked = _roll_user_session(state, kickout=True)
        device_id, model = _device(state)
        if force:
            # vehicle/auth 会重新签发并改变到期时间；代价是可能注销手机上的车辆 token。
            print("[refresh] 强制重新签发车辆 token")
            state["accounts"] = issue_accounts(state["sessionKey"], state["userId"], device_id, model)
            save_state(state)
            return state
        if not state.get("accounts"):
            state["accounts"] = issue_accounts(state["sessionKey"], state["userId"], device_id, model)
            save_state(state)
            return state

        failed = False
        for account in state["accounts"]:
            ec = account.get("enterpriseCode", "")
            account_id = account.get("accountId", "")
            try:
                entry = omp_vehicle_refresh(state["sessionKey"], state["userId"], ec, device_id, model, account_id)
            except (OmpError, RuntimeError) as exc:
                # 10011 且尚未 kickout：主动顶一次再重试
                if isinstance(exc, OmpError) and str(exc.code) == "10011" and not kicked:
                    try:
                        kicked_payload = omp_user_kickout(state["sessionKey"], state["userId"], device_id, model)
                        if status_of(kicked_payload) != "0":
                            raise RuntimeError("user/kickout 未把会话转为受信") from exc
                        state["sessionKey"] = kicked_payload.get("sessionKey") or state["sessionKey"]
                        kicked = True
                        save_state(state)
                        entry = omp_vehicle_refresh(state["sessionKey"], state["userId"], ec,
                                                    device_id, model, account_id)
                    except (OmpError, RuntimeError) as exc2:
                        print(f"[warn] {ec} vehicle/refresh 仍失败: {str(exc2)[:120]}")
                        failed = True
                        break
                else:
                    print(f"[warn] {ec} vehicle/refresh 失败: {str(exc)[:120]}")
                    failed = True
                    break
            account["vehicleAccessToken"] = entry.get("accessToken") or account.get("vehicleAccessToken", "")
            account["vehicleTokenExpiresAt"] = normalize_expires(entry.get("expiresAt")) or account.get(
                "vehicleTokenExpiresAt", 0)
            state["updatedAtUnix"] = int(time.time())
            save_state(state)

        if failed:
            # 兜底：重新走一次 vehicle/auth 全量签发（会注销旧 token）
            print("[refresh] 单品牌续期失败，改用 vehicle/auth 全量重签")
            state["accounts"] = issue_accounts(state["sessionKey"], state["userId"], device_id, model)
            save_state(state)
    return state


# ---------- 状态存取 ----------

def load_state() -> dict | None:
    try:
        state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save_state(state: dict) -> None:
    tmp = SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)
    os.chmod(SESSION_FILE, 0o600)


# ---------- 车辆云 ----------

class PinnedAdapter(HTTPAdapter):
    """品牌 CA 缺失时：跳过链验证，但核对叶子证书指纹。"""

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        kwargs["cert_reqs"] = ssl.CERT_NONE
        kwargs["assert_hostname"] = False
        super().init_poolmanager(connections, maxsize, block, **kwargs)


def _verify_pin(response: requests.Response, *args, **kwargs) -> None:
    sock = getattr(getattr(response.raw, "_connection", None), "sock", None)
    if sock is None:
        return
    try:
        der = sock.getpeercert(binary_form=True)
    except (ValueError, OSError):
        return
    if der is not None and hashlib.sha256(der).hexdigest() != SERES_LEAF_SHA256:
        raise requests.exceptions.SSLError("对端证书指纹不匹配（疑似中间人），已中止。")


def brand_session(enterprise_code: str) -> tuple[str, requests.Session]:
    ec = (enterprise_code or "SERES").upper()
    host, ca_name = BRANDS["SERES"][:2]
    for key, (brand_host, brand_ca, _name) in BRANDS.items():
        if key in ec:
            host, ca_name = brand_host, brand_ca
            break
    ca = CERT_DIR / ca_name
    if ca.is_file():
        session = requests.Session()
        session.verify = str(ca)
        return host, session
    if host == BRANDS["SERES"][0]:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session = requests.Session()
        session.verify = False
        session.mount("https://", PinnedAdapter())
        session.hooks["response"].append(_verify_pin)
        return host, session
    raise RuntimeError(f"缺少品牌 CA 证书：{ca}")


def ivcs_headers(token: str, device_id: str, model: str, vehicle_id: str | None = None) -> dict:
    headers = {
        "Authorization": token,
        "X-Nonce": str(uuid.uuid4()).upper(),
        "X-Created": time.strftime("%Y%m%d%H%M%S%f", time.localtime())[:-3],
        "X-Device-Id": device_id,
        "X-App-Id": "0",
        "X-Client-Model": model,
        "X-Client-Language": "zh-Hans",
        "X-Client-Type": "2",
        "X-Client-Version": "HUAWEI_IVCS_APP_3.002.300",
        "Accept": "*/*",
        "Accept-Language": "zh-Hans-CN;q=1",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    if vehicle_id:
        headers["X-Vehicle-Id"] = str(vehicle_id)
    return headers


def token_invalid(response: requests.Response) -> bool:
    if response.status_code == 401:
        return True
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        if str(body.get("code")) in ("100011", "10011", "100015"):
            return True
        message = str(body.get("msg") or body.get("message") or "")
        lower = message.lower()
        if ("token" in lower and ("invalid" in lower or "expired" in lower)) or \
                any(marker in message for marker in ("失效", "未登录", "登录过期")):
            return True
    return any(marker in response.text[:500] for marker in ("100015", "10011"))


def _parse_json(response: requests.Response):
    response.raise_for_status()
    return response.json()


def fetch_vehicles(state: dict, account: dict) -> list[dict]:
    host, session = brand_session(account.get("enterpriseCode", ""))
    response = session.get(
        f"{host}/vcam/v1/accounts/vehicles",
        headers=ivcs_headers(account["vehicleAccessToken"], state["deviceId"], state["deviceModel"]),
        timeout=30,
    )
    if token_invalid(response):
        raise TokenInvalid()
    data = _parse_json(response)
    return data if isinstance(data, list) else []


def fetch_dynamic(state: dict, account: dict, vehicle_id: str) -> dict:
    host, session = brand_session(account.get("enterpriseCode", ""))
    response = session.post(
        f"{host}/vctrl/v2/controls/dynamic-infos",
        json={key: "" for key in DYNAMIC_KEYS},
        headers=ivcs_headers(account["vehicleAccessToken"], state["deviceId"],
                             state["deviceModel"], vehicle_id),
        timeout=30,
    )
    if token_invalid(response):
        raise TokenInvalid()
    return _parse_json(response)


def _ok_response(response: requests.Response) -> dict:
    if response.status_code == 200 and "json" in response.headers.get("Content-Type", ""):
        try:
            return {"ok": True, "data": response.json()}
        except ValueError:
            pass
    return {"ok": False, "http": response.status_code, "body": response.text[:300]}


def fetch_full(state: dict, account: dict, vehicle: dict) -> dict:
    """旧 data.py --what all 的只读类别；inquiry 会唤醒车辆，故意不包含。"""
    vehicle_id = str(vehicle.get("vehicleId"))
    host, session = brand_session(account.get("enterpriseCode", ""))
    token = account["vehicleAccessToken"]
    renewed = False

    def call(method: str, base: str, path: str, body: dict | None = None) -> dict:
        nonlocal state, account, token, host, session, renewed
        url = f"{host}/{base}{path}"
        for _ in range(2):
            headers = ivcs_headers(token, state["deviceId"], state["deviceModel"], vehicle_id)
            if method == "GET":
                response = session.get(url, headers=headers, timeout=30)
            else:
                response = session.post(url, json=body or {}, headers=headers, timeout=30)
            if token_invalid(response) and not renewed:
                state, account = refresh_and_get_account(state, account)
                token = account["vehicleAccessToken"]
                host, session = brand_session(account.get("enterpriseCode", ""))
                renewed = True
                continue
            return _ok_response(response)
        return _ok_response(response)

    return {
        "vehicle": {
            "enterpriseCode": account.get("enterpriseCode"),
            "plateNo": vehicle.get("plateNo"),
            "vin": vehicle.get("vin"),
            "vehicleId": vehicle.get("vehicleId"),
        },
        "vehicles": call("GET", "vcam", "/v1/accounts/vehicles"),
        "account": {
            "base": call("GET", "vcam", "/v1/accounts/base"),
            # /accounts/simple 需要 App 内账号搜索参数 searchInfo，不属于车辆数据，未纳入。
            "sales": call("POST", "vcam", "/v1/accounts/sales/query", {}),
            "device": call("POST", "vcam", "/v1/accounts/vehicle-infos/device/query",
                           {"type": 3, "vehicleId": vehicle_id}),
        },
        "status": call("POST", "vctrl", "/v2/controls/dynamic-infos",
                       {key: "" for key in DYNAMIC_KEYS}),
        "protectConfig": call("GET", "vctrl", "/v1/controls/protect/config"),
        "maintenance": {
            "history": call("POST", "vcam", "/v1/vehicle-use/repair/maintain/history", {}),
            "latest": call("POST", "vcam", "/v1/vehicle-use/repair/maintain/latest", {}),
        },
        "reservations": call("POST", "vcam",
                             "/v1/vehicle-use/repair/reservation/operation/query/list", {}),
        "sim": call("GET", "vcam", "/v1/mno/sim/data/usage"),
        "dashcam": call("GET", "vcam", "/v1/dashcams/media"),
        "geofence": call("GET", "vctrl", "/v1/geofences"),
    }


def refresh_and_get_account(state: dict, account: dict) -> tuple[dict, dict]:
    state = refresh_state(state)
    for candidate in state.get("accounts") or []:
        if candidate.get("enterpriseCode") == account.get("enterpriseCode") and \
                candidate.get("accountId") == account.get("accountId"):
            return state, candidate
    return state, (state.get("accounts") or [account])[0]


def ensure_vehicle_token(state: dict, account: dict) -> tuple[dict, dict]:
    """车辆 token 剩余不足 1 小时就先刷新，避免请求时才发现失效。"""
    expiry = normalize_expires(account.get("vehicleTokenExpiresAt"))
    if not account.get("vehicleAccessToken") or (expiry and expiry <= time.time() + 3600):
        return refresh_and_get_account(state, account)
    return state, account


# ---------- 登录 ----------

def auth_url() -> tuple[str, str]:
    state = secrets.token_hex(8)
    params = {
        "access_type": "offline", "response_type": "code", "client_id": CLIENT_ID,
        "ui_locales": "zh-cn", "redirect_uri": REDIRECT_URI, "state": state,
        "scope": SCOPE, "display": "touch", "nonce": secrets.token_hex(8),
        "include_granted_scopes": "true", "uuid": str(uuid.uuid4()),
        "themeName": "huawei", "cVersion": "HwID_6.13.0.300",
    }
    return OAUTH_AUTHORIZE + "?" + urlencode(params), state


def extract_code(raw: str) -> str:
    raw = raw.strip()
    if "code=" in raw:
        query = raw.split("?", 1)[1] if "?" in raw else raw
        return parse_qs(query).get("code", [""])[0]
    return raw


def cmd_login(args) -> None:
    if args.url:
        print(auth_url()[0])
        return
    if args.code:
        raw = args.code
    else:
        url, _ = auth_url()
        print("在浏览器打开下面的链接（已登录则免短信）：\n")
        print(url)
        print("\n登录/同意后会跳到打不开的 xop 页面；")
        print("把地址栏里的整条 URL（或其中的 code）粘贴到这里，回车：")
        try:
            raw = input("> ").strip()
        except EOFError:
            raise SystemExit("已取消")
    code = extract_code(raw)
    if not code:
        raise SystemExit("没有解析到 code，请粘贴含 code=... 的完整回调 URL。")

    with renew_lock():
        state = login_with_code(code)
        save_state(state)

    print(f"✓ 登录成功：userId={state['userId']}")
    print(f"  会话到期：{_fmt_time(state['sessionKeyExpiresAtUnix'])}")
    for account in state["accounts"]:
        print(f"  {_brand_name(account['enterpriseCode'])} ({account['enterpriseCode']}) "
              f"车辆 token 到期 {_fmt_time(account['vehicleTokenExpiresAt'])}")
        try:
            for vehicle in fetch_vehicles(state, account):
                print(f"    {vehicle.get('plateNo') or '-'}  VIN {vehicle.get('vin') or '-'}  "
                      f"vehicleId {vehicle.get('vehicleId')}")
        except Exception as exc:  # 列表失败不影响登录结果
            print(f"    [warn] 车辆列表获取失败: {str(exc)[:100]}")
    print(f"会话已保存到 {SESSION_FILE}（0600）")


# ---------- 命令 ----------

def _fmt_time(ts) -> str:
    ts = int(ts or 0)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _brand_name(enterprise_code: str) -> str:
    ec = (enterprise_code or "SERES").upper()
    for key, (_host, _ca, name) in BRANDS.items():
        if key in ec:
            return name
    return "问界"


def _load_or_die() -> dict:
    state = load_state()
    if not state:
        raise SystemExit(f"没有会话，请先运行：python3 {PROG} login")
    return state


def _vehicles_with_renew(state: dict, account: dict) -> tuple[dict, dict, list[dict]]:
    state, account = ensure_vehicle_token(state, account)
    try:
        return state, account, fetch_vehicles(state, account)
    except TokenInvalid:
        state, account = refresh_and_get_account(state, account)
        return state, account, fetch_vehicles(state, account)


def cmd_refresh(args) -> None:
    state = _load_or_die()
    if args.session_only:
        state = refresh_user_only(state)
        print(f"✓ 用户会话已滚动（未签发车辆 token，不会踢手机）："
              f"到期 {_fmt_time(state['sessionKeyExpiresAtUnix'])}")
        return
    state = refresh_state(state, force=args.force)
    print(f"✓ 已刷新：userId={state['userId']}  会话到期 {_fmt_time(state['sessionKeyExpiresAtUnix'])}")
    for account in state.get("accounts") or []:
        print(f"  {_brand_name(account['enterpriseCode'])} ({account['enterpriseCode']}) "
              f"车辆 token 到期 {_fmt_time(account['vehicleTokenExpiresAt'])}")


def _vehicle_rows(state: dict) -> tuple[dict, list[tuple[dict, dict]]]:
    """按账号顺序展开车辆，序号与 vehicles 输出一致。"""
    if not state.get("accounts"):
        state = refresh_state(state)
    rows: list[tuple[dict, dict]] = []
    for account in list(state.get("accounts") or []):
        state, account, vehicles = _vehicles_with_renew(state, account)
        for vehicle in vehicles:
            rows.append((account, vehicle))
    return state, rows


def _select_vehicle(state: dict, vehicle_id: str | None = None, index: int | None = None):
    state, rows = _vehicle_rows(state)
    if not rows:
        raise SystemExit("账号下没有车辆")
    if vehicle_id:
        for account, vehicle in rows:
            if str(vehicle.get("vehicleId")) == str(vehicle_id):
                return state, account, vehicle
        raise SystemExit(f"没有找到车辆 {vehicle_id}")
    if index is not None:
        if not 1 <= index <= len(rows):
            raise SystemExit(f"序号 {index} 超出范围（共 {len(rows)} 辆）")
        account, vehicle = rows[index - 1]
        return state, account, vehicle
    account, vehicle = rows[0]
    return state, account, vehicle


def cmd_vehicles(_args) -> None:
    state = _load_or_die()
    state, rows = _vehicle_rows(state)
    if not rows:
        raise SystemExit("账号下没有车辆")
    for index, (account, vehicle) in enumerate(rows, 1):
        print(f"[{index}] {_brand_name(account.get('enterpriseCode', ''))}·"
              f"{account.get('enterpriseCode')}  {vehicle.get('plateNo') or '-'}  "
              f"VIN {vehicle.get('vin') or '-'}  vehicleId {vehicle.get('vehicleId')}")


def _gear(value) -> str:
    try:
        return GEARS.get(int(value), str(value) if value is not None else "-")
    except (TypeError, ValueError):
        return str(value)


def print_summary(vehicle: dict, data: dict) -> None:
    vs = data.get("vehicleStatus") or {}
    charge = data.get("charge") or {}
    location = data.get("location") or {}
    loc = location.get("location") or {}
    door = data.get("door") or {}
    hvac = data.get("hvac") or {}
    tire = data.get("tire") or {}
    fuel = data.get("fuel") or {}

    def g(d, key):
        value = d.get(key)
        return "-" if value is None else value

    def num(value, digits=1):
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return "-" if value is None else str(value)

    def labeled(value, mapping):
        if value is None:
            return "-"
        try:
            text = mapping.get(int(value), "未知")
        except (TypeError, ValueError):
            text = "未知"
        return f"{text} ({value})"

    gear_value = vs.get("gearSignal")
    gear_text = f"{_gear(gear_value)} ({gear_value})" if gear_value is not None else "-"

    print(f"车辆 {g(vehicle, 'plateNo')}  VIN {g(vehicle, 'vin')}  vehicleId {g(vehicle, 'vehicleId')}")
    print(f"里程 {num(vs.get('totalMileage'))} km  速度 {num(vs.get('speed'))} km/h  "
          f"档位 {gear_text}")
    remaining = vs.get("sumRemainingMileage")
    if remaining is None:
        remaining = charge.get("vcuSumRemainingMileage")
    remaining_text = "-" if remaining is None else remaining

    def charge_number(key):
        try:
            return float(charge.get(key))
        except (TypeError, ValueError):
            return None

    def charge_int(key):
        try:
            return int(charge.get(key))
        except (TypeError, ValueError):
            return 0

    ac_status = charge_int("acChargeStatus")
    dc_status = charge_int("dcChargeStatus")
    status = max(ac_status, dc_status)
    gun = max(charge_int("chargeConStatus"), charge_int("dcChargeGunConnectStatus"))
    status_labels = {
        0: "未充电", 1: "准备充电", 5: "充电异常", 6: "充电中",
        7: "充电结束", 18: "充电中（绝缘检测）", 25: "预约充电",
    }
    status_text = status_labels.get(status, "未知")
    if status == 0 and gun not in (0, 1, -1):
        status_text = "已连接未充电"

    dc_current = charge_number("dcChargeCurrent")
    ac_current = charge_number("acChargeCurrent")
    current = charge_number("chargeCurrent")
    if dc_current is not None and dc_current > 0:
        current = dc_current
    elif ac_current is not None and ac_current > 0:
        current = ac_current
    elif dc_status in (6, 18, 25) and dc_current is not None:
        current = dc_current
    elif ac_status in (6, 18, 25) and ac_current is not None:
        current = ac_current

    voltage = charge_number("chargeVoltage")
    power = charge_number("chargePower")
    if (power is None or power <= 0) and voltage and current and current > 0:
        power = voltage * current / 1000

    def charge_unit(value, unit):
        return "-" if value is None else f"{value:.1f} {unit}"

    remain = charge_number("remainChargeTime")
    if remain is None or remain <= 0:
        remain_text = "-"
    else:
        minutes = int(remain)
        if minutes > 1440:
            remain_text = "24+ 小时"
        elif minutes < 60:
            remain_text = f"{minutes} 分钟"
        else:
            hours, minutes = divmod(minutes, 60)
            remain_text = f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")

    print(f"电池 SOC {g(vs, 'soc')}%  续航 {remaining_text} km")
    print(f"充电 状态 {status_text} ({status})  电流 {charge_unit(current, 'A')}  "
          f"电压 {charge_unit(voltage, 'V')}  功率 {charge_unit(power, 'kW')}  "
          f"剩余 {remain_text}")
    print(f"位置 {g(loc, 'longitude')},{g(loc, 'latitude')}  海拔 {g(location, 'altitude')} m  "
          f"方向 {g(location, 'direction')}°  卫星 {g(location, 'sateliteNum')}")
    tires = "  ".join(
        f"{label} {g(tire.get(key) or {}, 'pressure')}"
        for key, label in (("leftFront", "左前"), ("rightFront", "右前"),
                           ("leftBack", "左后"), ("rightBack", "右后"))
    )
    print(f"胎压 {tires} bar")

    lock_labels = {0: "锁闭", 1: "开锁"}
    open_labels = {0: "关闭", 1: "打开"}
    print(f"门窗 中控锁 {labeled(door.get('centerLock'), lock_labels)}  "
          f"后备箱 {labeled(door.get('trunk'), open_labels)}  "
          f"充电口盖 {labeled(door.get('chargeHood'), open_labels)}  "
          f"前舱盖 {labeled(door.get('frondHood'), open_labels)}")

    def temp(value):
        return "-" if value is None else f"{float(value) / 10:.1f}°C"

    ac_text = labeled(hvac.get("acStatus"), open_labels)
    print(f"空调 状态 {ac_text}  车内 {temp(hvac.get('insideTemp'))}  "
          f"车外 {temp(hvac.get('outsideTemp'))}  "
          f"PM2.5 {g(hvac, 'insidePm25')}/{g(hvac, 'outsidePm25')}")
    if fuel.get("tankVolume") or fuel.get("leftVolume"):
        print(f"油量 {g(fuel, 'leftPercent')}%  油箱 {g(fuel, 'leftVolume')} L")


def cmd_info(args) -> None:
    state = _load_or_die()
    state, account, vehicle = _select_vehicle(state, args.vehicle_id, args.index)

    if args.full:
        print(json.dumps(fetch_full(state, account, vehicle), ensure_ascii=False, indent=2))
        return

    vehicle_id = str(vehicle.get("vehicleId"))
    try:
        data = fetch_dynamic(state, account, vehicle_id)
    except TokenInvalid:
        state, account = refresh_and_get_account(state, account)
        data = fetch_dynamic(state, account, vehicle_id)

    print_summary(vehicle, data)


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文算 2 列）。"""
    display = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - display)


HELP = "\n".join([
    "HIMA Info — 鸿蒙智行车辆数据工具（只读）",
    "",
    "用法:",
    f"  python3 {PROG} <命令> [参数]",
    "",
    "命令:",
    "  " + _pad("长命令", 14) + _pad("短命令", 9) + "说明",
    "  " + _pad("login", 14) + _pad("l", 9) + "生成授权链接并保存会话",
    "  " + _pad("refresh", 14) + _pad("r", 9) + "刷新用户会话和车辆 token",
    "  " + _pad("vehicles", 14) + _pad("v", 9) + "车辆列表（带序号）",
    "  " + _pad("info", 14) + _pad("i", 9) + "实时车辆信息（默认第一辆车）",
    "",
    "示例:",
    f"  python3 {PROG} info",
    f"  python3 {PROG} info 2",
    f"  python3 {PROG} info 2 --full",
    f"  python3 {PROG} refresh --session-only",
    f"  python3 {PROG} login --url",
    "",
    "查看命令参数:",
    f"  python3 {PROG} <命令> -h",
]) + "\n"


class HimaParser(argparse.ArgumentParser):
    def __init__(self, *args, top_level: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.top_level = top_level

    def format_help(self) -> str:
        return HELP if self.top_level else super().format_help()

    def error(self, message: str) -> None:
        if message.startswith("argument cmd: invalid choice:"):
            message = "未知命令 " + message.removeprefix("argument cmd: invalid choice:").split(" (choose from")[0].strip()
        print(f"错误：{message}\n")
        self.print_help()
        raise SystemExit(2)


def main() -> None:
    parser = HimaParser(description="HIMA Info — 鸿蒙智行车辆数据工具（只读）", top_level=True)
    sub = parser.add_subparsers(dest="cmd")

    p_login = sub.add_parser("login", aliases=["l"], help="生成授权链接并保存会话")
    p_login.add_argument("--url", action="store_true", help="只打印授权链接，不登录")
    p_login.add_argument("--code", help="非交互：直接传回调 URL 或 code")
    p_login.set_defaults(func=cmd_login)

    p_refresh = sub.add_parser("refresh", aliases=["r"], help="刷新用户会话和车辆 token")
    refresh_mode = p_refresh.add_mutually_exclusive_group()
    refresh_mode.add_argument("--session-only", action="store_true",
                              help="只滚动用户会话，不签发车辆 token（推荐定时保活，不会踢手机）")
    refresh_mode.add_argument("--force", action="store_true",
                              help="强制重新签发车辆 token（到期时间会变，可能踢手机）")
    p_refresh.set_defaults(func=cmd_refresh)

    p_vehicles = sub.add_parser("vehicles", aliases=["v"], help="车辆列表（带序号）")
    p_vehicles.set_defaults(func=cmd_vehicles)

    p_info = sub.add_parser("info", aliases=["i"], help="实时车辆信息（默认第一辆车）")
    p_info.add_argument("index", nargs="?", type=int, metavar="序号",
                        help="vehicles 列表中的序号，如 info 2")
    p_info.add_argument("--vehicle-id", metavar="ID", help="指定 vehicleId")
    p_info.add_argument("--full", action="store_true",
                        help="获取全部只读数据并输出 JSON（车辆/账号/状态/保养/预约/SIM/行车记录/围栏）")
    p_info.set_defaults(func=cmd_info)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return
    try:
        args.func(args)
    except NeedsLogin as exc:
        raise SystemExit(str(exc))
    except (OmpError, RuntimeError, requests.RequestException) as exc:
        raise SystemExit(f"失败：{str(exc)[:300]}")


if __name__ == "__main__":
    main()
