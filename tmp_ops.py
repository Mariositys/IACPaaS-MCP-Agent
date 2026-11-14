import json
import sqlite3

conn = sqlite3.connect("data/chat.db")
for message_id, question, answer, calls in conn.execute(
    "SELECT id, question, answer, calls FROM messages ORDER BY id DESC LIMIT 4"
):
    print("---", message_id, question)
    print(json.loads(answer).get("answer", "") if answer else "")
    print("CALLS", json.dumps(json.loads(calls or "[]"), ensure_ascii=False)[:4000])
