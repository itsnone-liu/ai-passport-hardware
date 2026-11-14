"""Entry point:  python3 -m server --config server/config/example.json

Mock mode (no API key, no network) is the default on purpose: the service must be
runnable and testable before anyone has credentials, and "it works locally" must
not depend on a paid API being reachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from server.config import Config
from server.service import Service
from server.asr.mock import MockASR
from server.tts.mock import MockTTS
from server.dsh.mock import MockBrain


def build_backends(cfg: Config):
    if cfg.asr == "bailian":
        from server.asr.bailian import BailianRealtimeASR
        asr = BailianRealtimeASR(cfg.bailian_api_key, cfg.bailian_base_url,
                                 cfg.bailian_asr_model)
    else:
        asr = MockASR()

    if cfg.tts == "bailian":
        from server.tts.bailian import BailianTTS
        tts = BailianTTS(cfg.bailian_api_key, cfg.bailian_base_url,
                         cfg.bailian_tts_model, cfg.bailian_voice)
    else:
        tts = MockTTS()

    if cfg.brain == "dsh":
        if not cfg.dsh_url:
            raise SystemExit("brain=dsh requires dsh_url; see server/dsh/README.md")
        from server.dsh.http import DshHTTPBrain
        brain = DshHTTPBrain(cfg.dsh_url, cfg.dsh_token)
    elif cfg.brain == "dsh-cli":
        # Measured 5.1 s per turn on this host (node boot ~2.5 s + model round
        # trip). Fine for notifications, slow for conversation -- see
        # server/dsh/README.md before making it the default.
        from server.dsh.cli import DshCLIBrain
        brain = DshCLIBrain(profile=cfg.dsh_profile, binary=cfg.dsh_binary)
    else:
        brain = MockBrain()
    return asr, tts, brain


async def run(cfg: Config) -> None:
    asr, tts, brain = build_backends(cfg)
    svc = Service(cfg, asr, tts, brain)
    await svc.start()
    log = logging.getLogger("passport")
    log.info("ready: control=%d audio=%d asr=%s tts=%s brain=%s devices=%s",
             svc.control_port, svc.audio_port, asr.name, tts.name, brain.name,
             ",".join(cfg.devices) or "(none)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # pragma: no cover - Windows
            pass
    await stop.wait()
    await svc.stop()
    log.info("stopped")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m server")
    ap.add_argument("--config", default=os.environ.get("PASSPORT_CONFIG", ""))
    ap.add_argument("--host", default=None)
    ap.add_argument("--control-port", type=int, default=None)
    ap.add_argument("--audio-port", type=int, default=None)
    ap.add_argument("--asr", choices=["mock", "bailian"], default=None)
    ap.add_argument("--tts", choices=["mock", "bailian"], default=None)
    ap.add_argument("--brain", choices=["mock", "dsh", "dsh-cli"], default=None)
    ap.add_argument("--device", action="append", default=[],
                    metavar="ID=SECRET", help="devices that may connect (repeatable)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env(args.config)
    for name in ("host", "control_port", "audio_port", "asr", "tts", "brain"):
        value = getattr(args, name)
        if value is not None:
            setattr(cfg, name, value)
    for item in args.device:
        device_id, _, secret = item.partition("=")
        cfg.devices[device_id] = secret.encode()

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())