"""Detect EC2 instance metadata via IMDSv2 (with sane fallback)."""

from __future__ import annotations

import platform
import socket

import httpx

IMDS = "http://169.254.169.254"


async def detect_instance() -> dict:
    """Return {instance_type, instance_id, region, az, arch}.

    Falls back to local hostname / uname when not running on EC2.
    """
    info: dict = {
        "instance_type": "unknown",
        "instance_id": socket.gethostname(),
        "region": "unknown",
        "az": "unknown",
        "arch": platform.machine(),
    }

    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            tok = (
                await c.put(
                    f"{IMDS}/latest/api/token",
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                )
            ).text
            h = {"X-aws-ec2-metadata-token": tok}
            info["instance_type"] = (await c.get(f"{IMDS}/latest/meta-data/instance-type", headers=h)).text
            info["instance_id"] = (await c.get(f"{IMDS}/latest/meta-data/instance-id", headers=h)).text
            info["az"] = (await c.get(f"{IMDS}/latest/meta-data/placement/availability-zone", headers=h)).text
            info["region"] = info["az"][:-1] if info["az"] else "unknown"
    except Exception:
        # Not on EC2 or IMDS unreachable - keep fallback values
        pass

    return info
