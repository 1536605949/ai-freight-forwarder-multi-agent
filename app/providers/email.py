from __future__ import annotations
from dataclasses import dataclass
from email.message import EmailMessage
import smtplib
from app.config import get_settings

@dataclass
class SendResult: message_id:str; provider:str
class EmailProvider:
    async def send(self,to:str,subject:str,body:str)->SendResult: raise NotImplementedError
class MockEmailProvider(EmailProvider):
    async def send(self,to,subject,body): return SendResult(message_id='mock-'+str(abs(hash((to,subject,body))))[:12],provider='mock')
class SMTPEmailProvider(EmailProvider):
    async def send(self,to,subject,body):
        s=get_settings(); msg=EmailMessage(); msg['From']=s.smtp_from; msg['To']=to; msg['Subject']=subject; msg.set_content(body)
        with smtplib.SMTP(s.smtp_host,s.smtp_port,timeout=20) as smtp: smtp.send_message(msg)
        return SendResult(message_id=msg.get('Message-ID') or 'smtp',provider='smtp')
def get_email_provider(): return SMTPEmailProvider() if get_settings().email_provider=='smtp' else MockEmailProvider()
