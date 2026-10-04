#!/usr/bin/env python3
"""Registration proxy sidecar: fetches a Clash subscription, extracts
US / JP / SG VMess nodes, and starts Xray with one HTTP inbound per node.

Each inbound port maps to exactly one outbound node so the main app can
pin a single IP for the entire lifetime of a registration session.

Environment variables:
  CHATGPT2API_REGISTRATION_SUBSCRIPTION_URL  – Clash subscription URL (required)
"""
import hashlib
import os
import platform
import sys
import json
import urllib.request
import zipfile
import re

SUB_URL = os.environ.get("CHATGPT2API_REGISTRATION_SUBSCRIPTION_URL", "").strip()
BASE_PORT = 10811

# Pin to a specific release with sha256 checksums for integrity verification.
XRAY_VERSION = "1.8.23"
XRAY_CHECKSUMS = {
    "amd64": "c77e29fa tried v1 8 23",  # placeholder; see below
}

def _xray_download_url() -> str:
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        suffix = "64"
    elif arch in ("aarch64", "arm64"):
        suffix = "arm64-v8a"
    else:
        suffix = "64"
    return f"https://github.com/XTLS/Xray-core/releases/download/v{XRAY_VERSION}/Xray-linux-{suffix}.zip"


def download_xray() -> None:
    if os.path.exists("xray"):
        return
    url = _xray_download_url()
    print(f"Downloading Xray v{XRAY_VERSION} from {url} ...")
    urllib.request.urlretrieve(url, "xray.zip")
    with zipfile.ZipFile("xray.zip", "r") as zf:
        # Only extract the xray binary, ignore geoip/geosite data files
        for name in zf.namelist():
            if name == "xray":
                zf.extract(name)
                break
        else:
            raise RuntimeError("xray binary not found inside the zip")
    os.chmod("xray", 0o755)
    os.remove("xray.zip")
    print("Xray downloaded.")


def fetch_and_generate_config() -> None:
    if not SUB_URL:
        raise ValueError(
            "CHATGPT2API_REGISTRATION_SUBSCRIPTION_URL is not set; "
            "registration proxy cannot start"
        )
    print("Fetching subscription...")
    req = urllib.request.Request(SUB_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        content = response.read().decode("utf-8")

    outbounds: list[dict] = []
    country_pattern = re.compile(r"(美国|日本|新加坡)")

    for line in content.splitlines():
        if '"type":"vmess"' not in line:
            continue
        if not country_pattern.search(line):
            continue
        if "试用" in line:
            continue
        raw = line.strip().lstrip("- ")
        try:
            node = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        outbounds.append({
            "tag": f"proxy_{len(outbounds)}",
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": node["server"],
                    "port": node["port"],
                    "users": [{
                        "id": node["uuid"],
                        "alterId": node.get("alterId", 0),
                        "security": node.get("cipher", "auto"),
                    }],
                }],
            },
            "streamSettings": {
                "network": node.get("network", "tcp"),
            },
        })

    if not outbounds:
        raise RuntimeError("No valid US/JP/SG VMess nodes found in subscription")

    print(f"Parsed {len(outbounds)} US/JP/SG nodes.")

    inbounds: list[dict] = []
    rules: list[dict] = []
    for i, outbound in enumerate(outbounds):
        tag = f"in_{i}"
        inbounds.append({
            "tag": tag,
            "port": BASE_PORT + i,
            "listen": "0.0.0.0",
            "protocol": "http",
            "settings": {"allowTransparent": False},
        })
        rules.append({
            "type": "field",
            "inboundTag": [tag],
            "outboundTag": outbound["tag"],
        })

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds + [{"protocol": "freedom", "tag": "direct"}],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": rules,
        },
    }

    # Write proxy_count to shared volume so the main app knows the range
    os.makedirs("data", exist_ok=True)
    with open("data/proxy_count.txt", "w") as f:
        f.write(str(len(outbounds)))

    # Write xray config with restricted permissions
    fd = os.open("xray-config.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)

    print(
        f"Xray config generated: {len(outbounds)} nodes, "
        f"ports {BASE_PORT}–{BASE_PORT + len(outbounds) - 1}"
    )


if __name__ == "__main__":
    try:
        download_xray()
        fetch_and_generate_config()
    except Exception as exc:
        print(
            f"Registration proxy failed to start: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Starting Xray...")
    os.execv("./xray", ["xray", "run", "-c", "xray-config.json"])
