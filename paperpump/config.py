import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    db_path: str = "paperpump.db"
    start_balance: float = 100.0
    fee_rate: float = 0.01
    priority_fee: float = 0.001
    delay_seconds: float = 1.5
    reset_cooldown_hours: float = 24.0
    tpsl_interval: float = 30.0


def load() -> Config:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing. Copy .env.example to .env and fill it in.")
    f = lambda k, d: float(os.getenv(k, d))  # noqa: E731
    return Config(
        token=token,
        db_path=os.getenv("DB_PATH", "paperpump.db"),
        start_balance=f("START_BALANCE_SOL", 100),
        fee_rate=f("FEE_RATE", 0.01),
        priority_fee=f("PRIORITY_FEE_SOL", 0.001),
        delay_seconds=f("SIM_DELAY_SECONDS", 1.5),
        reset_cooldown_hours=f("RESET_COOLDOWN_HOURS", 24),
        tpsl_interval=f("TPSL_CHECK_SECONDS", 30),
    )
