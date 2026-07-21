"""LINE webhook server — รับข้อความ/ไฟล์จาก LINE แล้วส่งต่อให้ Claude จัดการ Drive"""

import mimetypes
import os
import threading
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, abort, request, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
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


def push_text(user_id: str, text: str, image_urls: list[str] | None = None) -> None:
    msgs = [TextMessage(text=text[:4900])]
    for u in (image_urls or [])[:4]:
        msgs.append(ImageMessage(original_content_url=u, preview_image_url=u))
    with ApiClient(line_config) as api:
        MessagingApi(api).push_message(PushMessageRequest(to=user_id, messages=msgs))


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


# คำเรียกบอทในแชทกลุ่ม เช่น "บอท หาไฟล์ x" (แชทเดี่ยวไม่ต้องใช้)
TRIGGER = os.environ.get("BOT_TRIGGER", "บอท")

# จำชื่อไฟล์ล่าสุดที่ถูกส่งในแต่ละกลุ่ม (message_id -> ชื่อไฟล์) ไว้ใช้ตอนมีคน reply สั่งเก็บ
_recent_group_files: dict[str, dict] = defaultdict(dict)
_RECENT_FILES_MAX = 100

# กัน LINE ส่ง webhook ซ้ำ (redelivery เปิดอยู่ — cold start ทำให้ reply ช้าจน LINE คิดว่า timeout แล้วส่งซ้ำ)
# ถ้าไม่กัน จะมี 2 thread รัน Gemini + อ่านไฟล์ใหญ่พร้อมกัน แรมพุ่งเป็นสองเท่า
_seen_message_ids: set[str] = set()
_seen_lock = threading.Lock()


def _already_processing(message_id: str) -> bool:
    with _seen_lock:
        if message_id in _seen_message_ids:
            return True
        _seen_message_ids.add(message_id)
        if len(_seen_message_ids) > 500:
            _seen_message_ids.clear()
        return False


def _strip_self_mention(event, text: str):
    """ถ้าข้อความ @mention ตัวบอท คืนข้อความที่ตัดส่วน mention ออกแล้ว; ไม่ได้ mention คืน None"""
    mention = getattr(event.message, "mention", None)
    if not mention or not mention.mentionees:
        return None
    for m in mention.mentionees:
        if getattr(m, "is_self", False):
            return (text[:m.index] + text[m.index + m.length:]).strip()
    return None


def _target_id(event) -> tuple[str, bool]:
    """คืน (id ปลายทางสำหรับ push/ประวัติแชท, เป็นแชทกลุ่มไหม)"""
    src = event.source
    if src.type == "group":
        return src.group_id, True
    if src.type == "room":
        return src.room_id, True
    return src.user_id, False


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    if _already_processing(event.message.id):
        return  # LINE ส่ง webhook นี้ซ้ำ (redelivery) — กำลังประมวลผลรอบแรกอยู่แล้ว

    target, is_group = _target_id(event)
    text = event.message.text.strip()

    # ในกลุ่ม: ตอบเฉพาะเมื่อถูก @mention หรือขึ้นต้นด้วยคำเรียก — ใครเรียกก็ได้
    if is_group:
        mentioned = _strip_self_mention(event, text)
        if mentioned is not None:
            text = mentioned
        elif text.startswith(TRIGGER):
            text = text[len(TRIGGER):].lstrip(" ,:").strip()
        else:
            return
        if not text:
            reply_text(event.reply_token,
                       f"เรียกผมได้เลยครับ เช่น \"{TRIGGER} หาไฟล์ราคาประเมิน\" หรือแท็กผมพร้อมคำถาม")
            return

    if text in ("/reset", "เริ่มใหม่"):
        bot.reset(target)
        reply_text(event.reply_token, "ล้างประวัติสนทนาแล้วครับ")
        return

    # โมเดลอาจใช้เวลาคิดนานกว่า reply token จะทัน —
    # ตอบรับก่อน แล้วประมวลผลใน background ส่งผลผ่าน push message
    reply_text(event.reply_token, "รับทราบ กำลังดำเนินการ...")

    quoted_id = getattr(event.message, "quoted_message_id", None)
    done = threading.Event()

    def heartbeat():
        # ถ้างานเกิน 75 วิ แจ้งผู้ใช้ว่ายังทำอยู่ (งานหลายไฟล์รอบแรกใช้เวลาหลายนาที)
        if not done.wait(75):
            try:
                push_text(target, "ยังดำเนินการอยู่ครับ งานนี้ต้องอ่านข้อมูลหลายไฟล์ "
                                  "อาจใช้เวลา 2-5 นาที เดี๋ยวส่งผลให้ทันทีที่เสร็จ")
            except Exception:  # noqa: BLE001
                pass

    def work():
        prefix = ""
        # ผู้ใช้ reply ถึงไฟล์ในกลุ่มแล้วสั่งบอท -> ดึงไฟล์นั้นอัปขึ้น Drive ให้ก่อน
        if is_group and quoted_id:
            try:
                with ApiClient(line_config) as api:
                    data = MessagingApiBlob(api).get_message_content(quoted_id)
                fname = _recent_group_files.get(target, {}).get(quoted_id, "ไฟล์แนบจากแชท")
                mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
                f = drive_tools.upload_bytes(bytes(data), fname, mime)
                prefix = (f"(ระบบ: ผู้ใช้ตอบกลับถึงไฟล์แนบในแชท ระบบบันทึกลง Drive แล้ว "
                          f"ชื่อ '{f['name']}' file_id={f['id']} ลิงก์ {f.get('webViewLink', '')} "
                          f"— ทำตามคำสั่งของผู้ใช้ต่อ เช่น ยืนยันการเก็บ ย้ายโฟลเดอร์ หรือสรุปเนื้อหา) ")
            except Exception as e:  # noqa: BLE001
                prefix = ("(ระบบ: ผู้ใช้ตอบกลับถึงไฟล์แนบแต่ระบบดึงไฟล์ไม่สำเร็จ "
                          f"({e}) — แจ้งผู้ใช้ว่าไฟล์อาจเก่าเกินไปหรือถูกส่งก่อนบอทเข้ากลุ่ม "
                          "ให้ลองส่งไฟล์นั้นเข้ามาใหม่) ")
        try:
            answer = bot.chat(target, prefix + text)
        except Exception as e:  # noqa: BLE001
            answer = f"เกิดข้อผิดพลาด: {e}"
        done.set()
        push_text(target, answer, drive_tools.pop_pending_images())

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=work, daemon=True).start()


@handler.add(MessageEvent, message=FileMessageContent)
@handler.add(MessageEvent, message=ImageMessageContent)
def on_file(event):
    user_id, is_group = _target_id(event)
    if is_group:
        # ในกลุ่มไม่อัปโหลดอัตโนมัติ (กันไฟล์คุยเล่นไหลลง Drive)
        # แต่จำชื่อไฟล์ไว้ เผื่อมีคน reply สั่ง "เก็บไฟล์นี้"
        fname = (event.message.file_name
                 if isinstance(event.message, FileMessageContent)
                 else f"image_{event.message.id}.jpg")
        files = _recent_group_files[user_id]
        files[event.message.id] = fname
        while len(files) > _RECENT_FILES_MAX:
            files.pop(next(iter(files)))
        return
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


@app.route("/img/<path:fname>")
def serve_image(fname):
    return send_from_directory(drive_tools.IMG_DIR, fname, mimetype="image/png")


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
