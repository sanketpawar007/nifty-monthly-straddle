"""
Reads the Kite access_token written daily by the Go autologin binary.
Cron: 15 3 * * 1-5  (03:15 UTC = 08:45 AM IST)
"""
from pathlib import Path
from utils.logger import get_logger

log = get_logger("token_manager")


class TokenManager:
    def __init__(self, token_file: str):
        self.token_file = Path(token_file)
        self._token: str = ""

    def load(self) -> str:
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"Access token file not found: {self.token_file}\n"
                "Run autologin: ./scripts/zerodha_autologin.sh"
            )
        token = self.token_file.read_text().strip()
        if not token:
            raise ValueError("Access token file is empty — autologin may have failed")
        self._token = token
        log.info("Access token loaded: %s...", token[:8])
        return token

    @property
    def token(self) -> str:
        if not self._token:
            self.load()
        return self._token

    def refresh(self) -> str:
        self._token = ""
        return self.load()
