import os
from datetime import datetime, date, time, timedelta
import threading
import asyncio
import calendar
from typing import Dict, Any, Optional, List, Tuple
import sys

from flask import Flask, request, jsonify, current_app
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView

from sqlalchemy.orm import joinedload
from sqlalchemy import func

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    JobQueue
)

from config import Config
from extensions import db
from models import Course, Participant, Session

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config.get('SECRET_KEY')

db.init_app(app)

admin = Admin(app, name='Учительская')
admin.add_view(ModelView(Participant, db.session, name='Участники'))
admin.add_view(ModelView(Session, db.session, name='Занятия'))
admin.add_view(ModelView(Course, db.session, name='Курсы'))

tgapp: Optional[Application] = None
jqu: Optional[JobQueue] = None

TEACHER_IDS = app.config.get('TEACHER_IDS', [])

def is_teacher(user_id: int) -> bool:
    return user_id in TEACHER_IDS

@app.route('/courses', methods=['POST'])
def create_course():
    data = request.json
    course = Course(name=data['name'], direction=data.get('direction', ''), group=data.get('group', ''))
    db.session.add(course)
    db.session.commit()
    return jsonify({"id": course.id, "name": course.name})

@app.route('/sessions', methods=['POST'])
def crsess():
    data = request.json
    sess = Session(
        course_id=data['course_id'],
        date_time=datetime.fromisoformat(data['date_time']),
        duration_minutes=data.get('duration_minutes', 90),
        instructor=data.get('instructor', ''),
        location=data.get('location', ''),
        status=data.get('status', 'planned'),
        five_min_warn_sent=False
    )
    db.session.add(sess)
    db.session.commit()
    return jsonify({"id": sess.id})

@app.route('/participants', methods=['POST'])
def addpart():
    data = request.json
    part = Participant(
        name=data['name'], 
        contact=data.get('contact', ''), 
        telegram_id=data.get('telegram_id'),
        notifications_enabled=data.get('notifications_enabled', True),
        warn_5_min=data.get('warn_5_min', False)
    )
    db.session.add(part)
    db.session.commit()
    return jsonify({"id": part.id})

@app.route('/sessions/<int:session_id>/register', methods=['POST'])
def regpartses(session_id):
    data = request.json
    part_id = data['participant_id']
    sess = Session.query.get_or_404(session_id)
    part = Participant.query.get_or_404(part_id)
    if part not in sess.participants:
        sess.participants.append(part)
        db.session.commit()
    return jsonify({"status": "registered"})

@app.route('/sessions/<int:session_id>', methods=['PUT'])
def update_session(session_id):
    data = request.json
    sess = Session.query.get_or_404(session_id)
    
    orig_dt = sess.date_time
    orig_status = sess.status
    orig_loc = sess.location
    orig_instr = sess.instructor

    has_changed = False

    if 'date_time' in data:
        new_dt = datetime.fromisoformat(data['date_time'])
        if new_dt != orig_dt:
            sess.date_time = new_dt
            sess.five_min_warn_sent = False
            has_changed = True

    if 'status' in data:
        if data['status'] != orig_status:
            sess.status = data['status']
            sess.five_min_warn_sent = False
            has_changed = True
    
    if 'comment' in data:
        if data['comment'] != sess.comment:
            sess.comment = data['comment']
            has_changed = True
    if 'duration_minutes' in data:
        if data['duration_minutes'] != sess.duration_minutes:
            sess.duration_minutes = data['duration_minutes']
            has_changed = True
    if 'instructor' in data:
        if data['instructor'] != orig_instr:
            sess.instructor = data['instructor']
            has_changed = True
    if 'location' in data:
        if data['location'] != orig_loc:
            sess.location = data['location']
            has_changed = True
    
    if has_changed:
        db.session.commit()
        n_msg = "Занятие было обновлено."
        if sess.status != orig_status and sess.status in ('canceled', 'rescheduled'):
            n_msg = f"Статус занятия изменен на: {sess.status.capitalize()}"
        elif sess.date_time != orig_dt:
            n_msg = "Время проведения занятия изменено."
        elif sess.location != orig_loc:
            n_msg = "Место проведения занятия изменено."
        elif sess.instructor != orig_instr:
            n_msg = "Преподаватель занятия изменен."
        
        if tgapp:
            tgapp.create_task(notpar(sess.id, n_msg))
    
    return jsonify({"id": sess.id})

@app.route('/sessions/<int:session_id>', methods=['GET'])
def get_session(session_id):
    sess = Session.query.get_or_404(session_id)
    return jsonify({
        "id": sess.id,
        "course_id": sess.course_id,
        "course_name": sess.course.name if sess.course else None,
        "date_time": sess.date_time.isoformat(),
        "duration_minutes": sess.duration_minutes,
        "instructor": sess.instructor,
        "location": sess.location,
        "status": sess.status,
        "comment": sess.comment,
        "five_min_warn_sent": sess.five_min_warn_sent,
        "participants": [{"id": p.id, "name": p.name} for p in sess.participants]
    })

@app.route('/schedule', methods=['GET'])
def get_schedule():
    sessions = Session.query.order_by(Session.date_time).all()
    res = []
    for s in sessions:
        res.append({
            "id": s.id,
            "course_id": s.course_id,
            "course_name": s.course.name if s.course else None,
            "date_time": s.date_time.isoformat(),
            "duration_minutes": s.duration_minutes,
            "instructor": s.instructor,
            "location": s.location,
            "status": s.status,
            "comment": s.comment,
            "five_min_warn_sent": s.five_min_warn_sent,
            "participants": [{"id": p.id, "name": p.name} for p in s.participants]
        })
    return jsonify(res)

async def notpar(session_id: int, msg: str):
    global tgapp

    if not tgapp:
        return

    def getspnotsync():
        with app.app_context():
            sess = Session.query.options(db.joinedload(Session.course), db.joinedload(Session.participants)).get(session_id)
            if not sess:
                return None, []

            s_info = {
                'course_name': sess.course.name if sess.course else 'Курс',
                'date_time': sess.date_time,
                'location': sess.location,
                'instructor': sess.instructor,
                'status': sess.status,
                'comment': sess.comment
            }
            to_notify = []
            for p in sess.participants:
                if p.telegram_id:
                    to_notify.append({
                        'telegram_id': p.telegram_id,
                        'name': p.name,
                        'notifications_enabled': p.notifications_enabled,
                    })
            return s_info, to_notify

    s_info, to_notify = await asyncio.to_thread(getspnotsync)

    if not s_info:
        return

    for p_data in to_notify:
        u_id = p_data['telegram_id']
        if p_data['notifications_enabled']:
            n_text = (
                f"Уведомление о занятии:\n"
                f"{msg}\n\n"
                f"Курс: {s_info['course_name']}\n"
                f"Дата и время: {s_info['date_time'].strftime('%d.%m.%Y %H:%M')}\n"
                f"Место: {s_info['location'] or 'Не указано'}\n"
                f"Инструктор: {s_info['instructor'] or 'Не указан'}"
            )
            if s_info['comment']:
                 n_text += f"\nКомментарий: {s_info['comment']}"

            try:
                await tgapp.bot.send_message(
                    chat_id=u_id,
                    text=n_text,
                    parse_mode='HTML'
                )
            except Exception as e:
                pass

TOKEN = app.config.get('TELEGRAM_BOT_TOKEN')
DEVELOPER_CHAT_ID = int(app.config.get('DEVELOPER_CHAT_ID'))
PROFILE_FIO, PROFILE_GROUP_COMPANY = range(2)
SUGGEST_IDEA_TEXT = range(10)

(
    ADD_SESSION_COURSE, ADD_SESSION_DATE, ADD_SESSION_TIME, ADD_SESSION_DURATION, 
    ADD_SESSION_INSTRUCTOR, ADD_SESSION_LOCATION, ADD_SESSION_COMMENT,
    MANAGE_SESSION_SELECT, MANAGE_SESSION_ACTION,
    EDIT_SESSION_DATE, EDIT_SESSION_TIME, EDIT_SESSION_STATUS,
    EDIT_SESSION_INSTRUCTOR, EDIT_SESSION_LOCATION, EDIT_SESSION_COMMENT,
    EDIT_SESSION_DURATION_MINUTES
) = range(100, 116) 

mainkeyb = ReplyKeyboardMarkup(
    [
        ["Профиль", "Расписание"],
        ["Настройка уведомлений"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

teachkeyb = ReplyKeyboardMarkup(
    [
        ["Добавить занятие", "Мои занятия"],
        ["Назад в главное меню"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

def getsetkeysync(user_id: int) -> InlineKeyboardMarkup:
    with app.app_context():
        part = Participant.query.filter_by(telegram_id=user_id).first()
        settings = {'notifications_enabled': True, 'warn_5_min': False}
        if part:
            settings['notifications_enabled'] = part.notifications_enabled
            settings['warn_5_min'] = part.warn_5_min
        
        n_text = "Выкл. уведомлений" if settings['notifications_enabled'] else "Вкл. уведомлений"
        warn_text = "Не предупреждать за 5 мин" if settings['warn_5_min'] else "Предупреждать за 5 мин до события"

        kb = [
            [InlineKeyboardButton(n_text, callback_data='toggle_notifications')],
            [InlineKeyboardButton(warn_text, callback_data='toggle_warning_time')],
            [InlineKeyboardButton("Предложить идею разработчику", callback_data='suggest_idea')],
        ]
        return InlineKeyboardMarkup(kb)

def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    kb = []
    kb.append([InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")])
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    kb.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
    my_cal = calendar.monthcalendar(year, month)
    for week in my_cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                cb_data = f"schedule_day_{year}_{month}_{day}"
                row.append(InlineKeyboardButton(str(day), callback_data=cb_data))
        kb.append(row)
    
    today = date.today()
    prev_m_y = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m_y = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1

    kb.append([
        InlineKeyboardButton("◀️", callback_data=f"calendar_nav_{prev_y}_{prev_m_y}"),
        InlineKeyboardButton("Сегодня", callback_data=f"schedule_day_{today.year}_{today.month}_{today.day}"),
        InlineKeyboardButton("▶️", callback_data=f"calendar_nav_{next_y}_{next_m_y}"),
    ])
    return InlineKeyboardMarkup(kb)

def getstatkey(current_status: str) -> InlineKeyboardMarkup:
    statuses = ['planned', 'completed', 'canceled', 'rescheduled']
    btns = []
    for status in statuses:
        emoji = "✅ " if status == current_status else ""
        btns.append(InlineKeyboardButton(f"{emoji}{status.capitalize()}", callback_data=f"set_session_status_{status}"))
    return InlineKeyboardMarkup([btns])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    g_msg = f"Привет, {user.mention_html()}! 👋\n" \
                       "Я твой личный помощник. Выбери действие из меню ниже:"
    
    kb_btns = [
        [KeyboardButton("Профиль"), KeyboardButton("Расписание")],
        [KeyboardButton("Настройка уведомлений")],
    ]
    if is_teacher(user.id):
        t_kb_row = [KeyboardButton("Меню преподавателя")]
        kb_btns.append(t_kb_row)
    
    kb = ReplyKeyboardMarkup(kb_btns, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_html(g_msg, reply_markup=kb)

async def profmen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    u_id = update.effective_user.id

    def get_part_from_db_sync():
        with app.app_context():
            return Participant.query.filter_by(telegram_id=u_id).first()
    
    part = await asyncio.to_thread(get_part_from_db_sync)

    if part:
        await update.message.reply_text(
            f"<b>Ваш профиль:</b>\n"
            f"<b>ФИО:</b> {part.name}\n"
            f"<b>Группа/Компания:</b> {part.contact if part.contact else 'Не указано'}\n",
            parse_mode='HTML',
            reply_markup=mainkeyb,
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Похоже, ваш профиль еще не заполнен. Давайте начнем!\n"
            "Пожалуйста, введите ваше ФИО (например, Иванов Иван Иванович):"
        )
        return PROFILE_FIO

async def askfiost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fio = update.message.text
    context.user_data['profile_fio'] = fio

    await update.message.reply_text(
        f"Отлично, {fio}! Теперь укажите вашу группу или компанию (например, P2023 или ООО 'Рога и Копыта'):"
    )
    return PROFILE_GROUP_COMPANY

async def askgrcmp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    u_id = update.effective_user.id
    grp_cmp = update.message.text
    fio = context.user_data.pop('profile_fio')

    def save_or_update_part_sync():
        with app.app_context():
            part = Participant.query.filter_by(telegram_id=u_id).first()
            if not part:
                part = Participant(
                    telegram_id=u_id, 
                    name=fio, 
                    contact=grp_cmp,
                    notifications_enabled=True,
                    warn_5_min=False
                )
                db.session.add(part)
            else:
                part.name = fio
                part.contact = grp_cmp
            db.session.commit()
            
            return {
                'name': part.name,
                'contact': part.contact
            }

    p_data_saved = await asyncio.to_thread(save_or_update_part_sync)

    await update.message.reply_text(
        f"<b>Ваш профиль успешно сохранен!</b>\n"
        f"<b>ФИО:</b> {p_data_saved['name']}\n"
        f"<b>Группа/Компания:</b> {p_data_saved['contact']}\n",
        parse_mode='HTML',
        reply_markup=mainkeyb,
    )
    return ConversationHandler.END

async def cancproff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание профиля отменено.", reply_markup=mainkeyb)
    return ConversationHandler.END

async def schent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = date.today()
    kb = build_calendar(today.year, today.month)
    await update.message.reply_text("Выберите дату:", reply_markup=kb)

async def calenhan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("calendar_nav_"):
        parts = data.split('_')
        year = int(parts[2])
        month = int(parts[3])
        new_kb = build_calendar(year, month)
        await query.edit_message_reply_markup(reply_markup=new_kb)
    elif data.startswith("schedule_day_"):
        parts = data.split('_')
        year = int(parts[2])
        month = int(parts[3])
        day = int(parts[4])
        sel_date = date(year, month, day)

        sch_info = await fetschapi(sel_date)
        
        if len(sch_info) > 4000:
            sch_info = sch_info[:3900] + "\n...\n(Сообщение слишком длинное, продолжение в админке или по запросу)"

        await query.edit_message_text(
            f"<b>Расписание на {sel_date.strftime('%d.%m.%Y')}:</b>\n"
            f"{sch_info}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("<< Календарь", callback_data=f"calendar_nav_{year}_{month}")]])
        )
    elif data == "ignore":
        pass

async def fetschapi(sel_date: date) -> str:

    def getsessfdsync():
        with app.app_context():
            start_of_day = datetime.combine(sel_date, time.min)
            end_of_day = datetime.combine(sel_date, time.max)
            
            sessions = Session.query.filter(
                Session.date_time >= start_of_day,
                Session.date_time <= end_of_day
            ).order_by(Session.date_time).all()
            
            s_data = []
            for s in sessions:
                c_name = s.course.name if s.course else "Неизвестный курс"
                s_data.append({
                    "time": s.date_time.strftime('%H:%M'),
                    "duration": s.duration_minutes,
                    "course_name": c_name,
                    "instructor": s.instructor,
                    "location": s.location,
                    "status": s.status,
                    "comment": s.comment
                })
            return s_data

    s_data = await asyncio.to_thread(getsessfdsync)

    if not s_data:
        return "На этот день занятий нет! 🎉"
    
    res_str = ""
    for s_info in s_data:
        res_str += (
            f"<b>{s_info['time']}</b> ({s_info['duration']} мин.) - {s_info['course_name']}\n"
            f"  <i>Инструктор:</i> {s_info['instructor'] or 'Не указан'}\n"
            f"  <i>Место:</i> {s_info['location'] or 'Не указано'}\n"
            f"  <i>Статус:</i> {s_info['status']}\n"
        )
        if s_info['comment']:
            res_str += f"  <i>Комментарий:</i> {s_info['comment']}\n"
        res_str += "\n"
    return res_str

async def settings_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u_id = update.effective_user.id
    kb = getsetkeysync(u_id)
    await update.message.reply_text("Ваши настройки уведомлений:", reply_markup=kb)

async def sett(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()

    u_id = update.effective_user.id

    def gettogsett(u_id: int, setting_name: str):
        with app.app_context():
            part = Participant.query.filter_by(telegram_id=u_id).first()
            if part:
                current_val = getattr(part, setting_name)
                new_val = not current_val
                setattr(part, setting_name, new_val)
                db.session.commit()
                return new_val
            return None

    if query.data == 'toggle_notifications':
        new_val = await asyncio.to_thread(gettogsett, u_id, 'notifications_enabled')
        if new_val is not None:
            status_text = "включены" if new_val else "выключены"
            await query.edit_message_text(
                f"Уведомления теперь {status_text}.\nВаши настройки уведомлений:",
                reply_markup=getsetkeysync(u_id)
            )
        else:
            await query.edit_message_text("Не удалось обновить настройки уведомлений. Профиль не найден.")
    elif query.data == 'toggle_warning_time':
        new_val = await asyncio.to_thread(gettogsett, u_id, 'warn_5_min')
        if new_val is not None:
            status_text = "за 5 минут до события" if new_val else "не будут"
            await query.edit_message_text(
                f"Бот будет предупреждать {status_text}.\nВаши настройки уведомлений:",
                reply_markup=getsetkeysync(u_id)
            )
        else:
            await query.edit_message_text("Не удалось обновить настройки времени предупреждения. Профиль не найден.")
    elif query.data == 'suggest_idea':
        await query.message.reply_text("Напишите вашу идею или предложение разработчику. Я передам ее.",
                                       reply_markup=ReplyKeyboardMarkup([['Отмена']], resize_keyboard=True, one_time_keyboard=True))
        return SUGGEST_IDEA_TEXT
    
    return ConversationHandler.END

async def recidd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    idea_text = update.message.text

    try:
        await context.bot.send_message(
            chat_id=DEVELOPER_CHAT_ID,
            text=f"<b>Новая идея от пользователя {user.mention_html()} (ID: {user.id}):</b>\n\n{idea_text}",
            parse_mode='HTML'
        )
        await update.message.reply_text("Спасибо за вашу идею! Я передал ее разработчику. 🚀", reply_markup=mainkeyb)
    except Exception as e:
        await update.message.reply_text("Извините, произошла ошибка при отправке вашей идеи. Попробуйте позже.", reply_markup=mainkeyb)

    return ConversationHandler.END

async def cancidconv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отправка идеи отменена.", reply_markup=mainkeyb)
    return ConversationHandler.END

async def teachmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_teacher(update.effective_user.id):
        await update.message.reply_text("У вас нет прав преподавателя.")
        return
    await update.message.reply_text("Добро пожаловать в меню преподавателя!", reply_markup=teachkeyb)

async def bckmen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Возвращаемся в главное меню.", reply_markup=mainkeyb)
    return ConversationHandler.END 

async def addsstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_teacher(update.effective_user.id):
        await update.message.reply_text("У вас нет прав преподавателя.")
        return ConversationHandler.END
    
    def getcoursync():
        with app.app_context():
            return Course.query.order_by(Course.name).all()

    courses = await asyncio.to_thread(getcoursync)
    
    if not courses:
        await update.message.reply_text("Пока нет доступных курсов. Сначала добавьте курсы через админку.", reply_markup=teachkeyb)
        return ConversationHandler.END

    kb = [[InlineKeyboardButton(c.name, callback_data=f"add_session_course_{c.id}")] for c in courses]
    await update.message.reply_text("Выберите курс для занятия:", reply_markup=InlineKeyboardMarkup(kb))
    return ADD_SESSION_COURSE

async def addcourrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    c_id = int(query.data.split('_')[-1])
    context.user_data['new_session_course_id'] = c_id

    await query.edit_message_text("Введите дату занятия в формате ДД.ММ.ГГГГ (например, 01.01.2024):")
    return ADD_SESSION_DATE

async def adddaterec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        s_date = datetime.strptime(update.message.text, '%d.%m.%Y').date()
        context.user_data['new_session_date'] = s_date
        await update.message.reply_text("Введите время занятия в формате ЧЧ:ММ (например, 14:30):")
        return ADD_SESSION_TIME
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ.")
        return ADD_SESSION_DATE

async def addtimerec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        s_time = datetime.strptime(update.message.text, '%H:%M').time()
        s_date: date = context.user_data['new_session_date']
        s_dt = datetime.combine(s_date, s_time)
        
        context.user_data['new_session_datetime'] = s_dt
        await update.message.reply_text("Введите длительность занятия в минутах (например, 90):")
        return ADD_SESSION_DURATION
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ.")
        return ADD_SESSION_TIME

async def adddurrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dur = int(update.message.text)
        if dur <= 0:
            raise ValueError
        context.user_data['new_session_duration'] = dur
        await update.message.reply_text("Введите имя преподавателя (например, Смирнов П.А.):")
        return ADD_SESSION_INSTRUCTOR
    except ValueError:
        await update.message.reply_text("Неверный формат длительности. Пожалуйста, введите целое число минут.")
        return ADD_SESSION_DURATION

async def addinstrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_session_instructor'] = update.message.text
    await update.message.reply_text("Введите место проведения занятия (например, Аудитория 305):")
    return ADD_SESSION_LOCATION

async def addlocrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_session_location'] = update.message.text
    await update.message.reply_text("Введите любой дополнительный комментарий к занятию (или пропустите, введя '-'):")
    return ADD_SESSION_COMMENT

async def addcomrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comm = update.message.text
    context.user_data['new_session_comment'] = comm if comm != '-' else None

    c_id = context.user_data.get('new_session_course_id')
    s_dt = context.user_data.get('new_session_datetime')
    dur = context.user_data.get('new_session_duration')
    instr = context.user_data.get('new_session_instructor')
    loc = context.user_data.get('new_session_location')
    comm_final = context.user_data.get('new_session_comment')

    def crsess_sync(c_id, s_dt, dur, instr, loc, comm_final):
        with app.app_context():
            new_sess = Session(
                course_id=c_id,
                date_time=s_dt,
                duration_minutes=dur,
                instructor=instr,
                location=loc,
                comment=comm_final,
                status='planned',
                five_min_warn_sent=False
            )
            db.session.add(new_sess)
            db.session.commit()
            return new_sess.id, new_sess.course.name if new_sess.course else "Неизвестный курс"

    s_id, c_name = await asyncio.to_thread(
        crsess_sync, c_id, s_dt, dur, instr, loc, comm_final
    )

    await update.message.reply_text(
        f"Занятие успешно добавлено!\n"
        f"Курс: {c_name}\n"
        f"Дата и время: {s_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Инструктор: {instr}\n"
        f"Место: {loc}\n"
        f"Комментарий: {comm_final or 'Нет'}",
        reply_markup=teachkeyb
    )
    context.user_data.clear()
    return ConversationHandler.END

async def addcancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание занятия отменено.", reply_markup=teachkeyb)
    context.user_data.clear()
    return ConversationHandler.END

async def manage_sessions_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_teacher(update.effective_user.id):
        await update.message.reply_text("У вас нет прав преподавателя.")
        return ConversationHandler.END

    def get_upcoming_sessions_sync():
        with app.app_context():
            two_m_from_now = datetime.now() + timedelta(days=60)
            sessions = Session.query.options(db.joinedload(Session.course)).filter(
                Session.date_time >= datetime.now() - timedelta(hours=1),
                Session.date_time <= two_m_from_now,
                Session.status.in_(['planned', 'rescheduled'])
            ).order_by(Session.date_time).all()
            return sessions
    
    sessions = await asyncio.to_thread(get_upcoming_sessions_sync)

    if not sessions:
        await update.message.reply_text("Нет предстоящих занятий для управления.", reply_markup=teachkeyb)
        return ConversationHandler.END
    
    kb = []
    for s in sessions:
        c_name = s.course.name if s.course else "Неизвестный курс"
        s_text = f"{s.date_time.strftime('%d.%m.%Y %H:%M')} - {c_name} ({s.instructor or 'Без инструктора'})"
        kb.append([InlineKeyboardButton(s_text, callback_data=f"manage_session_{s.id}")])
    kb.append([InlineKeyboardButton("Отмена", callback_data="cancel_manage_session")])
    await update.message.reply_text("Выберите занятие для управления:", reply_markup=InlineKeyboardMarkup(kb))
    return MANAGE_SESSION_SELECT

async def managsel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    s_id = int(query.data.split('_')[-1])
    context.user_data['mngid'] = s_id

    def get_session_details_sync(s_id):
        with app.app_context():
            return Session.query.options(db.joinedload(Session.course)).get(s_id)

    sess = await asyncio.to_thread(get_session_details_sync, s_id)

    if not sess:
        await query.edit_message_text("Занятие не найдено или было удалено.", reply_markup=teachkeyb)
        context.user_data.clear()
        return ConversationHandler.END

    c_name = sess.course.name if sess.course else "Неизвестный курс"
    s_details = (
        f"<b>Выбрано занятие:</b>\n"
        f"<b>Курс:</b> {c_name}\n"
        f"<b>Дата и время:</b> {sess.date_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"<b>Длительность:</b> {sess.duration_minutes} мин.\n"
        f"<b>Инструктор:</b> {sess.instructor or 'Не указан'}\n"
        f"<b>Место:</b> {sess.location or 'Не указано'}\n"
        f"<b>Статус:</b> {sess.status.capitalize()}\n"
        f"<b>Комментарий:</b> {sess.comment or 'Нет'}\n\n"
        f"Что вы хотите изменить?"
    )

    kb = [
        [InlineKeyboardButton("Изменить дату/время", callback_data="edit_session_datetime")],
        [InlineKeyboardButton("Изменить длительность", callback_data="edit_session_duration")],
        [InlineKeyboardButton("Изменить статус", callback_data="edit_session_status")],
        [InlineKeyboardButton("Изменить инструктора", callback_data="edit_session_instructor")],
        [InlineKeyboardButton("Изменить место", callback_data="edit_session_location")],
        [InlineKeyboardButton("Изменить комментарий", callback_data="edit_session_comment")],
        [InlineKeyboardButton("Удалить занятие", callback_data="delete_session")],
        [InlineKeyboardButton("Отмена", callback_data="cancel_manage_session")]
    ]
    await query.edit_message_text(s_details, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    return MANAGE_SESSION_ACTION

async def editstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новую дату занятия в формате ДД.ММ.ГГГГ:")
    return EDIT_SESSION_DATE

async def editdaterec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_date = datetime.strptime(update.message.text, '%d.%m.%Y').date()
        context.user_data['new_edit_date'] = new_date
        await update.message.reply_text("Введите новое время занятия в формате ЧЧ:ММ:")
        return EDIT_SESSION_TIME
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ.")
        return EDIT_SESSION_DATE

async def edittimerec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_time = datetime.strptime(update.message.text, '%H:%M').time()
        s_id = context.user_data['mngid']
        old_date = context.user_data['new_edit_date']
        new_dt = datetime.combine(old_date, new_time)

        def update_sess_dt_sync(s_id, new_dt):
            with app.app_context():
                sess = Session.query.get(s_id)
                if sess:
                    old_dt = sess.date_time
                    sess.date_time = new_dt
                    sess.status = 'rescheduled' if sess.status == 'planned' and new_dt != old_dt else sess.status
                    sess.five_min_warn_sent = False
                    db.session.commit()
                    return sess.course.name if sess.course else "Курс"
                return None

        c_name = await asyncio.to_thread(update_sess_dt_sync, s_id, new_dt)

        if c_name:
            await update.message.reply_text(
                f"Дата и время занятия по курсу '{c_name}' успешно обновлены на {new_dt.strftime('%d.%m.%Y %H:%M')}.",
                reply_markup=teachkeyb
            )
            await notpar(s_id, f"Дата и время занятия изменены на: {new_dt.strftime('%d.%m.%Y %H:%M')}")
        else:
            await update.message.reply_text("Ошибка при обновлении занятия.", reply_markup=teachkeyb)
        
        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ.")
        return EDIT_SESSION_TIME

async def editdur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новую длительность занятия в минутах (например, 90):")
    return EDIT_SESSION_DURATION_MINUTES

async def editdurrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_dur = int(update.message.text)
        if new_dur <= 0:
            raise ValueError

        s_id = context.user_data['mngid']

        def update_sess_dur_sync(s_id, new_dur):
            with app.app_context():
                sess = Session.query.get(s_id)
                if sess:
                    sess.duration_minutes = new_dur
                    db.session.commit()
                    return sess.course.name if sess.course else "Курс"
                return None
        
        c_name = await asyncio.to_thread(update_sess_dur_sync, s_id, new_dur)

        if c_name:
            await update.message.reply_text(
                f"Длительность занятия по курсу '{c_name}' успешно обновлена на {new_dur} мин.",
                reply_markup=teachkeyb
            )
            await notpar(s_id, f"Длительность занятия изменена на: {new_dur} минут.")
        else:
            await update.message.reply_text("Ошибка при обновлении занятия.", reply_markup=teachkeyb)

        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Неверный формат длительности. Пожалуйста, введите целое число минут.")
        return EDIT_SESSION_DURATION_MINUTES

async def editstat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    s_id = context.user_data['mngid']

    def get_current_status_sync(s_id):
        with app.app_context():
            sess = Session.query.get(s_id)
            return sess.status if sess else 'planned'

    cur_status = await asyncio.to_thread(get_current_status_sync, s_id)
    kb = getstatkey(cur_status)
    await query.edit_message_text(f"Текущий статус: <b>{cur_status.capitalize()}</b>. Выберите новый статус:", parse_mode='HTML', reply_markup=kb)
    return EDIT_SESSION_STATUS

async def editstatss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    new_status = query.data.split('_')[-1]
    s_id = context.user_data['mngid']

    def update_sess_status_sync(s_id, new_status):
        with app.app_context():
            sess = Session.query.get(s_id)
            if sess:
                old_status = sess.status
                if old_status != new_status:
                    sess.status = new_status
                    if new_status in ['canceled', 'rescheduled']:
                        sess.five_min_warn_sent = False
                    db.session.commit()
                    return sess.course.name if sess.course else "Курс", old_status, new_status
            return None, None, None

    c_name, old_status, new_status_c = await asyncio.to_thread(update_sess_status_sync, s_id, new_status)

    if c_name:
        await query.message.reply_text(
            f"Статус занятия по курсу '{c_name}' успешно обновлен с '{old_status.capitalize()}' на '{new_status_c.capitalize()}'.",
            reply_markup=teachkeyb
        )
        await notpar(s_id, f"Статус занятия изменен на: {new_status_c.capitalize()}")
    else:
        await query.message.reply_text("Ошибка при обновлении статуса занятия.", reply_markup=teachkeyb)
    
    context.user_data.clear()

async def editteach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое имя преподавателя:")
    return EDIT_SESSION_INSTRUCTOR

async def editinstrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_instr = update.message.text
    s_id = context.user_data['mngid']

    def update_sess_instr_sync(s_id, new_instr):
        with app.app_context():
            sess = Session.query.get(s_id)
            if sess:
                sess.instructor = new_instr
                db.session.commit()
                return sess.course.name if sess.course else "Курс"
            return None

    c_name = await asyncio.to_thread(update_sess_instr_sync, s_id, new_instr)

    if c_name:
        await update.message.reply_text(
            f"Преподаватель занятия по курсу '{c_name}' успешно обновлен на '{new_instr}'.",
            reply_markup=teachkeyb
        )
        await notpar(s_id, f"Преподаватель занятия изменен на: {new_instr}")
    else:
        await update.message.reply_text("Ошибка при обновлении преподавателя занятия.", reply_markup=teachkeyb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def editloc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое место проведения занятия:")
    return EDIT_SESSION_LOCATION

async def editlocrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_loc = update.message.text
    s_id = context.user_data['mngid']

    def updlocsync(s_id, new_loc):
        with app.app_context():
            sess = Session.query.get(s_id)
            if sess:
                sess.location = new_loc
                db.session.commit()
                return sess.course.name if sess.course else "Курс"
            return None

    c_name = await asyncio.to_thread(updlocsync, s_id, new_loc)

    if c_name:
        await update.message.reply_text(
            f"Место проведения занятия по курсу '{c_name}' успешно обновлено на '{new_loc}'.",
            reply_markup=teachkeyb
        )
        await notpar(s_id, f"Место проведения занятия изменено на: {new_loc}")
    else:
        await update.message.reply_text("Ошибка при обновлении места проведения занятия.", reply_markup=teachkeyb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def editcom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый комментарий к занятию (или '-' чтобы очистить):")
    return EDIT_SESSION_COMMENT

async def editcomrec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_comm = update.message.text
    s_id = context.user_data['mngid']

    def update_sess_comm_sync(s_id, new_comm):
        with app.app_context():
            sess = Session.query.get(s_id)
            if sess:
                sess.comment = new_comm if new_comm != '-' else None
                db.session.commit()
                return sess.course.name if sess.course else "Курс"
            return None

    c_name = await asyncio.to_thread(update_sess_comm_sync, s_id, new_comm)

    if c_name:
        await update.message.reply_text(
            f"Комментарий к занятию по курсу '{c_name}' успешно обновлен.",
            reply_markup=teachkeyb
        )
        await notpar(s_id, f"Комментарий к занятию обновлен.")
    else:
        await update.message.reply_text("Ошибка при обновлении комментария к занятию.", reply_markup=teachkeyb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def delconf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    s_id = context.user_data['mngid']

    def getdelsync(s_id):
        with app.app_context():
            return Session.query.options(db.joinedload(Session.course)).get(s_id)
    
    sess_to_del = await asyncio.to_thread(getdelsync, s_id)

    if not sess_to_del:
        await query.edit_message_text("Занятие не найдено или уже удалено.", reply_markup=teachkeyb)
        context.user_data.clear()
        return ConversationHandler.END
    
    c_name = sess_to_del.course.name if sess_to_del.course else "Курс"
    s_dt = sess_to_del.date_time.strftime('%d.%m.%Y %H:%M')
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete_session_{s_id}")],
        [InlineKeyboardButton("Нет, отмена", callback_data="cancel_manage_session")]
    ])
    await query.edit_message_text(
        f"Вы действительно хотите удалить занятие по курсу '{c_name}' {s_dt}? Это действие необратимо.",
        reply_markup=kb
    )
    return MANAGE_SESSION_ACTION

async def delssexec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    s_id = int(query.data.split('_')[-1])

    def delsync(s_id):
        with app.app_context():
            sess = Session.query.options(db.joinedload(Session.course)).get(s_id)
            if sess:
                c_name = sess.course.name if sess.course else "Курс"
                s_dt = sess.date_time.strftime('%d.%m.%Y %H:%M')
                db.session.delete(sess)
                db.session.commit()
                return c_name, s_dt
            return None, None

    c_name, s_dt = await asyncio.to_thread(delsync, s_id)

    try:
        await query.delete_message()
    except Exception as e:
        pass

    if c_name:
        await query.message.reply_text(
            f"Занятие по курсу '{c_name}' ({s_dt}) успешно удалено.",
            reply_markup=teachkeyb
        )
        await notpar(s_id, f"Занятие по курсу '{c_name}' ({s_dt}) было отменено (удалено).")
    else:
        await query.message.reply_text("Ошибка при удалении занятия или оно уже было удалено.", reply_markup=teachkeyb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancelss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.message.reply_text("Управление занятиями отменено.", reply_markup=teachkeyb)
    
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Произошла ошибка. Пожалуйста, попробуйте еще раз.")
    if context.user_data:
        context.user_data.clear()
    if update.effective_message and update.effective_message.reply_markup:
        if is_teacher(update.effective_user.id):
            await update.effective_message.reply_text("Возвращаюсь в меню преподавателя.", reply_markup=teachkeyb)
        else:
            await update.effective_message.reply_text("Возвращаюсь в главное меню.", reply_markup=mainkeyb)

async def chkupcm(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    
    n_time_lb = now + timedelta(minutes=4, seconds=45)
    n_time_ub = now + timedelta(minutes=5, seconds=15)

    def get_sessions_for_warning_sync():
        with app.app_context():
            sessions = Session.query.options(db.joinedload(Session.course), db.joinedload(Session.participants)).filter(
                Session.date_time >= n_time_lb,
                Session.date_time <= n_time_ub,
                Session.status == 'planned',
                Session.five_min_warn_sent == False
            ).all()

            s_list = []
            for sess in sessions:
                parts_data = []
                for p in sess.participants:
                    if p.telegram_id:
                        parts_data.append({
                            'telegram_id': p.telegram_id,
                            'name': p.name,
                            'notifications_enabled': p.notifications_enabled,
                            'warn_5_min': p.warn_5_min
                        })
                if parts_data:
                    s_list.append({
                        'id': sess.id,
                        'course_name': sess.course.name if sess.course else 'Курс',
                        'date_time': sess.date_time,
                        'location': sess.location,
                        'instructor': sess.instructor,
                        'comment': sess.comment,
                        'participants': parts_data
                    })
            return s_list

    sessions_for_warning = await asyncio.to_thread(get_sessions_for_warning_sync)

    for s_info in sessions_for_warning:
        s_id = s_info['id']
        
        n_msg = (
            f"⚡️ <b>Занятие скоро начнется!</b> ⚡️\n\n"
            f"<b>Курс:</b> {s_info['course_name']}\n"
            f"<b>Когда:</b> {s_info['date_time'].strftime('%H:%M %d.%m.%Y')}\n"
            f"<b>Где:</b> {s_info['location'] or 'Не указано'}\n"
            f"<b>Инструктор:</b> {s_info['instructor'] or 'Не указан'}"
        )
        if s_info['comment']:
             n_msg += f"\n<b>Комментарий:</b> {s_info['comment']}"
        
        any_n_sent = False

        for p_data in s_info['participants']:
            u_id = p_data['telegram_id']
            if p_data['notifications_enabled'] and p_data['warn_5_min']:
                try:
                    await context.bot.send_message(
                        chat_id=u_id,
                        text=n_msg,
                        parse_mode='HTML'
                    )
                    any_n_sent = True
                except Exception as e:
                    pass
        
        if any_n_sent:
            def mark_session_warned_sync(s_id: int):
                with app.app_context():
                    sess = Session.query.get(s_id)
                    if sess:
                        sess.five_min_warn_sent = True
                        db.session.commit()
            await asyncio.to_thread(mark_session_warned_sync, s_id)

def runapiapp():
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)

def runbotapp():
    global tgapp, jqu
    tgapp = Application.builder().token(TOKEN).build()
    jqu = tgapp.job_queue
    tgapp.add_handler(CommandHandler("start", start))
    tgapp.add_handler(MessageHandler(filters.Regex("^Назад в главное меню$"), start))
    prof_conv_h = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Профиль$"), profmen)],
        states={
            PROFILE_FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, askfiost)],
            PROFILE_GROUP_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, askgrcmp)],
        },
        fallbacks=[CommandHandler("cancel", cancproff), MessageHandler(filters.Regex("^Отмена$"), cancproff)],
    )
    tgapp.add_handler(prof_conv_h)

    tgapp.add_handler(MessageHandler(filters.Regex("^Расписание$"), schent))
    tgapp.add_handler(CallbackQueryHandler(calenhan, pattern=r"^(calendar_nav_|schedule_day_|ignore)"))

    sett_conv_h = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Настройка уведомлений$"), settings_entry)],
        states={
            SUGGEST_IDEA_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Отмена$"), recidd)],
        },
        fallbacks=[CommandHandler("cancel", cancidconv), MessageHandler(filters.Regex("^Отмена$"), cancidconv)],
    )
    tgapp.add_handler(sett_conv_h)
    tgapp.add_handler(CallbackQueryHandler(sett, pattern=r"^(toggle_notifications|toggle_warning_time|suggest_idea)"))

    tgapp.add_handler(MessageHandler(filters.Regex("^Меню преподавателя$"), teachmenu))

    add_conv_h = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Добавить занятие$"), addsstart)],
        states={
            ADD_SESSION_COURSE: [CallbackQueryHandler(addcourrec, pattern=r"^add_session_course_\d+$")],
            ADD_SESSION_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddaterec)],
            ADD_SESSION_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtimerec)],
            ADD_SESSION_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddurrec)],
            ADD_SESSION_INSTRUCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, addinstrec)],
            ADD_SESSION_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addlocrec)],
            ADD_SESSION_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcomrec)],
        },
        fallbacks=[CommandHandler("cancel", addcancel), MessageHandler(filters.Regex("^Отмена$"), addcancel)],
        map_to_parent={ ConversationHandler.END: MANAGE_SESSION_SELECT }
    )
    tgapp.add_handler(add_conv_h)

    manage_conv_h = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Мои занятия$"), manage_sessions_start)],
        states={
            MANAGE_SESSION_SELECT: [
                CallbackQueryHandler(managsel, pattern=r"^manage_session_\d+$"),
                CallbackQueryHandler(cancelss, pattern=r"^cancel_manage_session$")
            ],
            MANAGE_SESSION_ACTION: [
                CallbackQueryHandler(editstart, pattern=r"^edit_session_datetime$"),
                CallbackQueryHandler(editdur, pattern=r"^edit_session_duration$"),
                CallbackQueryHandler(editstat, pattern=r"^edit_session_status$"),
                CallbackQueryHandler(editteach, pattern=r"^edit_session_instructor$"),
                CallbackQueryHandler(editloc, pattern=r"^edit_session_location$"),
                CallbackQueryHandler(editcom, pattern=r"^edit_session_comment$"),
                CallbackQueryHandler(delconf, pattern=r"^delete_session$"),
                CallbackQueryHandler(delssexec, pattern=r"^confirm_delete_session_\d+$"),
                CallbackQueryHandler(cancelss, pattern=r"^cancel_manage_session$"),

                MessageHandler(filters.TEXT & ~filters.COMMAND, editdaterec, block=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edittimerec, block=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editdurrec, block=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editinstrec, block=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editlocrec, block=False),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editcomrec, block=False),
                CallbackQueryHandler(editstatss, pattern=r"^set_session_status_")
            ],
            EDIT_SESSION_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editdaterec)],
            EDIT_SESSION_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edittimerec)],
            EDIT_SESSION_DURATION_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, editdurrec)],
            EDIT_SESSION_STATUS: [CallbackQueryHandler(editstatss, pattern=r"^set_session_status_")],
            EDIT_SESSION_INSTRUCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editinstrec)],
            EDIT_SESSION_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, editlocrec)],
            EDIT_SESSION_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, editcomrec)],
        },
        fallbacks=[CommandHandler("cancel", cancelss), MessageHandler(filters.Regex("^Отмена$"), cancelss)],
    )
    tgapp.add_handler(manage_conv_h)


    tgapp.add_error_handler(error_handler)

    jqu.run_repeating(chkupcm, interval=30, first=5) 

    tgapp.run_polling(allowed_updates=Update.ALL_TYPES)

def reset_database():
    with app.app_context():
        db.drop_all()
        db.create_all()

if __name__ == '__main__':
    req_part_attrs = ['telegram_id', 'notifications_enabled', 'warn_5_min']
    req_sess_attrs = ['five_min_warn_sent']

    all_attrs_present = True
    for attr in req_part_attrs:
        if not hasattr(Participant, attr):
            all_attrs_present = False
    for attr in req_sess_attrs:
        if not hasattr(Session, attr):
            all_attrs_present = False

    if not all_attrs_present:
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == 'reset_db':
        reset_database()
        sys.exit(0)
    
    with app.app_context():
        db.create_all()

    flask_thread = threading.Thread(target=runapiapp)
    flask_thread.start()
    runbotapp()