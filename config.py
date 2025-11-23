import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'sqdb.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', "8040223094:AAElyrJhhiWa0BNUruceJJcwgeYmoHk6Y68")
    DEVELOPER_CHAT_ID = os.environ.get('DEVELOPER_CHAT_ID', "1397562239")
    SECRET_KEY = os.environ.get('SECRET_KEY', 'super-root')
    TEACHER_IDS = [int(os.environ.get('DEVELOPER_CHAT_ID', "1397562239"))]