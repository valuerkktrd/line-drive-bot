"""ตัวกลาง: รับข้อความจาก LINE -> Gemini (function calling) -> คำตอบ"""

import os

from google import genai
from google.genai import types

from drive_tools import ALL_TOOLS

client = genai.Client()  # อ่าน GEMINI_API_KEY จาก env

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

SYSTEM_PROMPT = """คุณคือผู้ช่วยจัดการ Google Drive ผ่านแชท LINE ตอบเป็นภาษาไทย กระชับ ตรงประเด็น

กติกา:
- ใช้ tool ที่มีให้เพื่อค้นหา/จัดการไฟล์ใน Drive เมื่อผู้ใช้ถามหรือสั่ง
- อ่านเนื้อหาไฟล์ได้ด้วย read_file (Sheets/Docs/Excel/CSV/TXT) — ใช้เมื่อผู้ใช้อยากรู้ข้อมูลในไฟล์ สรุป หรือคำนวณตัวเลขจากไฟล์ (หา file_id จาก search_files/list_folder ก่อน)
- ก่อนลบไฟล์ (trash_file) ต้องบอกชื่อไฟล์ให้ผู้ใช้ยืนยันก่อนเสมอ ห้ามลบทันที
- เวลาแสดงรายการไฟล์ ให้บอกชื่อ + ลิงก์ (webViewLink) อ่านง่ายๆ ไม่ต้องโชว์ JSON ดิบ
- ถ้าหาไม่เจอ บอกตรงๆ และแนะนำคำค้นอื่น
- ข้อความจะแสดงใน LINE: ใช้ข้อความล้วน ไม่ใช้ markdown ตาราง หรือ code block"""

# chat session ต่อ user (SDK จัดการ history + เรียก tool อัตโนมัติ)
_chats: dict[str, object] = {}
MAX_TURNS = 40  # กัน history บวมเกิน


def chat(user_id: str, text: str) -> str:
    session = _chats.get(user_id)
    if session is None:
        session = client.chats.create(
            model=MODEL,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=ALL_TOOLS,  # ฟังก์ชัน Python ธรรมดา — SDK สร้าง schema และรัน loop ให้เอง
            ),
        )
        _chats[user_id] = session

    resp = session.send_message(text)

    if len(session.get_history()) > MAX_TURNS * 2:
        _chats.pop(user_id, None)  # เริ่ม session ใหม่รอบหน้า

    return (resp.text or "").strip() or "(ดำเนินการเสร็จแล้ว)"


def reset(user_id: str) -> None:
    _chats.pop(user_id, None)
