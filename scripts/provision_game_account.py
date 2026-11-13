"""Operator provisioning of an explicitly consented, existing game session.

No login/account import. Reads once to verify player identity before storage.
Input: raw protobuf GetInitialData request, supplied securely outside the repo.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(args):
    from dotenv import load_dotenv
    load_dotenv()
    import database
    from services import game_account
    from cryptography.fernet import Fernet

    if not args.owner_consented:
        raise game_account.AccountError("Сначала получите согласие владельца на подключение этой сессии.")
    game_account.require_pilot(args.owner, 1, "test")
    # Fail before opening the secret file if the owner/account is ineligible.
    game_account._identity(args.owner, 1, args.ally_code)
    key_path = Path(database.DB_NAME).resolve().parent / "cache" / "game-account.key"
    if not key_path.exists():
        with game_account._db() as conn:
            if conn.execute("SELECT 1 FROM game_account_sessions LIMIT 1").fetchone():
                raise game_account.AccountError("Ключ потерян. Восстановите исходный ключ из защищённой резервной копии.")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(Fernet.generate_key())
    source = Path(args.request_file)
    if source.stat().st_size > game_account.MAX_BYTES:
        raise game_account.AccountError("Файл сессии слишком большой.")
    await game_account.provision(args.owner, 1, "test", args.ally_code, args.provider,
                                 source.read_bytes(), consent=True)
    print("Сессия проверена и сохранена в зашифрованном виде. Автоматического входа нет.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--ally-code", required=True)
    parser.add_argument("--provider", choices=("ea", "google"), required=True)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--owner-consented", action="store_true")
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:
        # Never print exceptions from credential parsing or third-party clients.
        print("Подключение не выполнено. Проверьте согласие, права офицера АС, свой аликод и файл сессии.", file=sys.stderr)
        raise SystemExit(1)
