"""Lỗi dùng chung cho batch.py / judge.py / lark_base.py.

QuotaError -> batch chờ 30' rồi thử lại (tối đa 3 lần); AuthError -> dừng đợt ngay;
StepError(kind) -> ghi 'LỖI <ngày>: <kind>' cho nhóm rồi sang nhóm tiếp.
"""
import re


class QuotaError(RuntimeError):
    """Hết hạn mức codex / Claude / OpenArt."""


class AuthError(RuntimeError):
    """Hết đăng nhập codex / lark-cli / Claude."""


class StepError(RuntimeError):
    """Lỗi 1 bước của 1 nhóm. kind: 'tải ảnh' | 'judge' | 'gen' | 'up' | 'lark' | 'quá thời gian'."""

    def __init__(self, kind, msg=""):
        super().__init__(f"{kind}: {msg}" if msg else kind)
        self.kind = kind


QUOTA_RE = re.compile(
    r"HTTP 429|usage[_ ]limit|rate[_ ]limit|too many requests|quota|out of credits"
    r"|insufficient (credits|balance)|credit balance", re.I)
AUTH_RE = re.compile(
    r"token refresh failed|access token expired|refresh token expired|HTTP 401|\bunauthori[sz]ed"
    r"|run `?codex login|codex chưa đăng nhập|need_user_authorization|not logged in"
    r"|invalid api key|openart login", re.I)


def raise_for(text, kind):
    """Luôn ném lỗi: AuthError / QuotaError nếu nội dung khớp, không thì StepError(kind)."""
    t = (text or "").strip()
    if AUTH_RE.search(t):
        raise AuthError(t[-300:])
    if QUOTA_RE.search(t):
        raise QuotaError(t[-300:])
    raise StepError(kind, t[-300:])


def _selfcheck():
    def kind_of(text):
        try:
            raise_for(text, "gen")
        except AuthError:
            return "auth"
        except QuotaError:
            return "quota"
        except StepError as e:
            return e.kind
    assert kind_of('HTTP 429: {"error":{"type":"usage_limit_reached"}}') == "quota"
    assert kind_of("Claude AI usage limit reached|1760000000") == "quota"
    assert kind_of("token refresh failed: HTTPError") == "auth"
    assert kind_of("[vision   ] lỗi codex: codex chưa đăng nhập") == "auth"
    assert kind_of("RuntimeError: openart không tạo file") == "gen"
    assert kind_of("") == "gen"
    e = StepError("tải ảnh", "x")
    assert e.kind == "tải ảnh" and "x" in str(e)
    print("batch_errors selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
