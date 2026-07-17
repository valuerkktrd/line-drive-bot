"""LINE webhook server — รับข้อความ/ไฟล์จาก LINE แล้วส่งต่อให้ Claude จัดการ Drive"""

import os
import threading

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
)

import bot
import drive_tools

app = Flask(__name__)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
line_config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])


def push_text(user_id: str, text: str) -> None:
    with ApiClient(line_config) as api:
        MessagingApi(api).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text[:4900])])
        )


def reply_text(reply_token: str, text: str) -> None:
    with ApiClient(line_config) as api:
        MessagingApi(api).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token, messages=[TextMessage(text=text[:4900])]
            )
        )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ("/reset", "เริ่มใหม่"):
        bot.reset(user_id)
        reply_text(event.reply_token, "ล้างประวัติสนทนาแล้วครับ")
        return

    # Fable 5 อาจใช้เวลาคิดนานกว่า reply token จะทัน —
    # ตอบรับก่อน แล้วประมวลผลใน background ส่งผลผ่าน push message
    reply_text(event.reply_token, "รับทราบ กำลังดำเนินการ...")

    def work():
        try:
            answer = bot.chat(user_id, text)
        except Exception as e:  # noqa: BLE001
            answer = f"เกิดข้อผิดพลาด: {e}"
        push_text(user_id, answer)

    threading.Thread(target=work, daemon=True).start()


@handler.add(MessageEvent, message=FileMessageContent)
@handler.add(MessageEvent, message=ImageMessageContent)
def on_file(event):
    user_id = event.source.user_id
    reply_text(event.reply_token, "ได้รับไฟล์แล้ว กำลังอัปโหลดขึ้น Drive...")

    if isinstance(event.message, FileMessageContent):
        filename = event.message.file_name
        mime = "application/octet-stream"
    else:
        filename = f"image_{event.message.id}.jpg"
        mime = "image/jpeg"

    def work():
        try:
            with ApiClient(line_config) as api:
                data = MessagingApiBlob(api).get_message_content(event.message.id)
            f = drive_tools.upload_bytes(bytes(data), filename, mime)
            push_text(
                user_id,
                f"อัปโหลดแล้ว: {f['name']}\n{f.get('webViewLink', '')}",
            )
        except Exception as e:  # noqa: BLE001
            push_text(user_id, f"อัปโหลดไม่สำเร็จ: {e}")

    threading.Thread(target=work, daemon=True).start()


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
