"""rtl() bidi-safety — especially @mentions staying clickable."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/rtl.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.bot.rtl import rtl  # noqa: E402

FSI, PDI = "⁨", "⁩"


def test_mention_keeps_at_inside_isolate_so_it_stays_clickable():
    out = rtl("ربات: @jbdnbxscbot")
    # The @ MUST be inside the isolate; «@⁨name⁩» breaks Telegram's mention detection (not clickable).
    assert FSI + "@jbdnbxscbot" + PDI in out
    assert "@" + FSI not in out


def test_botfather_and_placeholder_mentions():
    assert FSI + "@BotFather" + PDI in rtl("به @BotFather بروید")
    assert FSI + "@yourID" + PDI in rtl("مثلاً @yourID را بفرستید")


def test_email_at_is_not_split_into_a_mention():
    # An email's @ is in the middle of a Latin run → the whole address stays one isolated unit.
    assert FSI + "user@example.com" + PDI in rtl("ایمیل user@example.com است")


def test_code_block_left_byte_exact():
    out = rtl("آدرس: <code>@notamention</code>")
    assert "<code>@notamention</code>" in out  # inside <code> nothing is touched
