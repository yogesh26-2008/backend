import httpx
from app.config import settings


async def send_otp_email(to_email: str, otp: str, name: str) -> None:
    """Send a 6-digit OTP verification email via Resend API."""

    if not settings.resend_api_key:
        print(f"[EMAIL] ⚠️  RESEND_API_KEY not set. OTP for {to_email}: {otp}")
        return

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Segoe UI',Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:40px 20px">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#111;border-radius:16px;border:1px solid #1e1e1e">
          <tr>
            <td style="padding:36px 40px 0">
              <div style="font-size:28px;font-weight:800;color:#fafafa;letter-spacing:2px">
                TRANDIA <span style="color:#6C63FF">✦</span>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 40px 0">
              <p style="color:#aaa;font-size:15px;margin:0">
                Namaste <strong style="color:#fafafa">{name}</strong>! 👋
              </p>
              <p style="color:#aaa;font-size:15px;margin:16px 0 0">
                Apna Trandia account verify karne ke liye neeche diya gaya OTP use karo:
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 40px">
              <div style="background:#1a1535;border:2px solid #6C63FF;border-radius:14px;
                          padding:28px;text-align:center">
                <div style="font-size:42px;font-weight:900;letter-spacing:12px;
                            color:#6C63FF;font-family:monospace">{otp}</div>
                <div style="color:#666;font-size:12px;margin-top:10px">
                  Yeh OTP <strong style="color:#aaa">10 minutes</strong> mein expire ho jaayega
                </div>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:0 40px 36px">
              <p style="color:#555;font-size:12px;margin:0;line-height:1.7">
                Agar aapne signup nahi kiya toh is email ko ignore karo.
                Kisi ke saath bhi yeh OTP share mat karo.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 40px;border-top:1px solid #1e1e1e">
              <p style="color:#333;font-size:11px;margin:0;text-align:center">
                © 2025 Trandia — Made with ✦ in India
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Trandia <onboarding@resend.dev>",
                "to": [to_email],
                "subject": f"Trandia — Your verification code is {otp} 🔐",
                "html": html,
            },
        )

    if response.status_code not in (200, 201):
        raise Exception(f"Resend API error {response.status_code}: {response.text}")
