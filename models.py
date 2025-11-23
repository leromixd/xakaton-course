from extensions import db
import datetime

participants_sessions = db.Table('participants_sessions',
    db.Column('participant_id', db.Integer, db.ForeignKey('participant.id')),
    db.Column('session_id', db.Integer, db.ForeignKey('session.id'))
)

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    direction = db.Column(db.String(64))
    group = db.Column(db.String(64))
    sessions = db.relationship('Session', backref='course', cascade='all, delete-orphan', lazy=True)

class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=True)
    name = db.Column(db.String(128), nullable=False)
    contact = db.Column(db.String(128))
    sessions = db.relationship('Session', secondary=participants_sessions, back_populates='participants') 
    notifications_enabled = db.Column(db.Boolean, default=True, nullable=False)
    warn_5_min = db.Column(db.Boolean, default=False, nullable=False)

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    date_time = db.Column(db.DateTime, nullable=False)
    duration_minutes = db.Column(db.Integer, default=90)
    instructor = db.Column(db.String(128))
    location = db.Column(db.String(128))
    status = db.Column(db.String(32), default='planned')
    comment = db.Column(db.Text)
    participants = db.relationship('Participant', secondary=participants_sessions, back_populates='sessions')
    five_min_warn_sent = db.Column(db.Boolean, default=False, nullable=False)