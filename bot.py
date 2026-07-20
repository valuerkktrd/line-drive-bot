"""ตัวกลาง: รับข้อความจาก LINE -> Gemini (function calling) -> คำตอบ"""

import os

from google import genai
from google.genai import types

from drive_tools import ALL_TOOLS

client = genai.Client()  # อ่าน GEMINI_API_KEY จาก env

# flash-lite: โควตาฟรีต่อวันสูงกว่า 3.5-flash มาก (3.5-flash ฟรีแค่ ~20 ครั้ง/วัน)
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

SYSTEM_PROMPT = """คุณคือผู้ช่วยจัดการ Google Drive ผ่านแชท LINE ตอบเป็นภาษาไทย กระชับ ตรงประเด็น

กติกา:
- ใช้ tool ที่มีให้เพื่อค้นหา/จัดการไฟล์ใน Drive เมื่อผู้ใช้ถามหรือสั่ง
- เอกสาร (PDF/Docs/Word รวมถึง PDF สแกน): ใช้ ask_document ส่งคำถามหรือ "สรุปสาระสำคัญ" — อ่านทั้งเล่ม
- อ่านเนื้อหาไฟล์เล็กด้วย read_file (TXT หรือตารางไม่กี่ร้อยแถว)
- ไฟล์ตารางขนาดใหญ่ (หลักพันถึงแสนแถว): เริ่มด้วย file_stats เพื่อดูชื่อคอลัมน์ แล้วใช้ query_file (ค้นหา/กรองแถว) หรือ aggregate_file (รวม/เฉลี่ย/นับ/สูงสุด/ต่ำสุด แยกกลุ่มได้) — ห้ามใช้ read_file กับไฟล์ใหญ่
- ไฟล์ Excel รองรับทั้ง .xlsx และ .xls แบบเก่า รวมถึง Google Sheets และ CSV — ระบบอ่านได้จริง
- ห้ามตอบว่า "อ่านไม่ได้/มีข้อจำกัด" โดยยังไม่ได้ลองเรียก tool — ให้เรียก file_stats หรือ read_file ก่อนเสมอ ถ้า tool คืน error ค่อยแจ้งผู้ใช้ตามจริง
- หา file_id จาก search_files/list_folder ก่อนเสมอ
- ทำกราฟจากข้อมูลจริงด้วย make_chart (bar/line/pie) — รูปถูกส่งเข้าแชทอัตโนมัติ
- กราฟที่รวมผลจากหลายไฟล์ (เช่น เทียบรายสาขา): คำนวณค่าจากแต่ละไฟล์ก่อน แล้วส่งตัวเลขให้ make_chart_from_data วาดรวมเป็นกราฟเดียว
- ทำภาพสรุปสวยงามด้วย make_infographic เมื่อผู้ใช้ขอ "infographic" — เตือนผู้ใช้ได้ว่าข้อความไทยในภาพอาจเพี้ยนบ้าง
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
