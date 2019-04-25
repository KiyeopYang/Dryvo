from datetime import datetime
from functools import wraps

import flask
import requests
from flask import Blueprint
from flask_login import current_user, login_required, logout_user
from loguru import logger

from server.api.database.models import (
    Day,
    Payment,
    Student,
    Teacher,
    User,
    WorkDay,
    PaymentType,
)
from server.api.push_notifications import FCM
from server.api.utils import jsonify_response, paginate
from server.consts import RECEIPT_URL, WORKDAY_DATE_FORMAT
from server.error_handling import RouteError

teacher_routes = Blueprint("teacher", __name__, url_prefix="/teacher")


def init_app(app):
    app.register_blueprint(teacher_routes)


def teacher_required(func):
    @wraps(func)
    def func_wrapper(*args, **kwargs):
        if not current_user.teacher:
            raise RouteError("User is not a teacher.", 401)

        return func(*args, **kwargs)

    return func_wrapper


def like_filter(model, key, value):
    return getattr(model, key).like(f"%{value}%")


@teacher_routes.route("/", methods=["GET"])
@jsonify_response
@paginate
def teachers():
    try:
        extra_filters = {User: {"name": like_filter}}
        query = Teacher.query.filter_by(is_approved=True)
        return Teacher.filter_and_sort(
            flask.request.args,
            extra_filters=extra_filters,
            query=query,
            with_pagination=True,
        )
    except ValueError:
        raise RouteError("Wrong parameters passed.")


@teacher_routes.route("/work_days", methods=["GET"])
@jsonify_response
@login_required
@teacher_required
def work_days():
    """ return work days with filter - only on a specific date,
    or with no date at all"""
    try:
        return {
            "data": [
                day.to_dict()
                for day in current_user.teacher.filter_work_days(flask.request.args)
            ]
        }
    except ValueError:
        raise RouteError("Wrong parameters passed.")


@teacher_routes.route("/work_days", methods=["POST"])
@jsonify_response
@login_required
@teacher_required
def update_work_days():
    data = flask.request.get_json()
    """ example data:
    0: [{from_hour: 8, from_minutes: 0, to_hour: 14}], 1: {}....
    OR
    "03-15-2019": [{from_hour: 8}], "03-16-2019": []....
    """
    for day, hours_list in data.items():
        # first, let's delete all current data with this date
        # TODO better algorithm for that
        try:
            day = int(day)
            params = dict(day=day)
            WorkDay.query.filter_by(**params).delete()
        except ValueError:
            # probably a date
            params = dict(on_date=datetime.strptime(day, WORKDAY_DATE_FORMAT))
            WorkDay.query.filter_by(**params).delete()

        for hours in hours_list:
            from_hour = max(min(int(hours.get("from_hour")), 24), 0)
            to_hour = max(min(int(hours.get("to_hour")), 24), 0)
            from_minutes = max(min(int(hours.get("from_minutes")), 60), 0)
            to_minutes = max(min(int(hours.get("to_minutes")), 60), 0)

            if from_hour >= to_hour:
                raise RouteError(
                    "There must be a bigger difference between the two times."
                )

            current_user.teacher.work_days.append(
                WorkDay(
                    from_hour=from_hour,
                    from_minutes=from_minutes,
                    to_hour=to_hour,
                    to_minutes=to_minutes,
                    **params,
                )
            )

    current_user.save()

    return {"message": "Days updated."}


@teacher_routes.route("/work_days/<int:day_id>", methods=["POST"])
@jsonify_response
@login_required
@teacher_required
def edit_work_day(day_id):
    day = current_user.teacher.work_days.filter_by(id=day_id).first()
    if not day:
        raise RouteError("Day does not exist", 404)
    data = flask.request.get_json()
    from_hour = data.get("from_hour", day.from_hour)
    to_hour = data.get("to_hour", day.to_hour)
    day.update(from_hour=from_hour, to_hour=to_hour)
    return {"message": "Day updated successfully."}


@teacher_routes.route("/work_days/<int:day_id>", methods=["DELETE"])
@jsonify_response
@login_required
@teacher_required
def delete_work_day(day_id):
    day = current_user.teacher.work_days.filter_by(id=day_id).first()
    if not day:
        raise RouteError("Day does not exist", 404)
    day.delete()
    return {"message": "Day deleted."}


@teacher_routes.route("/<int:teacher_id>/available_hours", methods=["POST"])
@jsonify_response
@login_required
def available_hours(teacher_id):
    data = flask.request.get_json()
    teacher = Teacher.get_by_id(teacher_id)
    duration = None
    if data.get("duration"):
        duration = int(data.get("duration"))
    only_approved = False
    if current_user.teacher:
        only_approved = True
    return {
        "data": list(
            teacher.available_hours(
                datetime.strptime(data.get("date"), "%Y-%m-%d"),
                duration,
                only_approved=only_approved,
            )
        )
    }


@teacher_routes.route("/add_payment", methods=["POST"])
@jsonify_response
@login_required
@teacher_required
def add_payment():
    data = flask.request.get_json()
    student = Student.get_by_id(data.get("student_id"))
    amount = data.get("amount")
    if not student:
        raise RouteError("Student does not exist.")
    if not amount:
        raise RouteError("Amount must be given.")

    payment = Payment.create(
        teacher=current_user.teacher,
        student=student,
        amount=amount,
        payment_type=getattr(PaymentType, data.get("payment_type", ""), 1),
        details=data.get("details"),
        crn=int(data.get("crn")) if data.get("crn") else None,
    )
    # send notification to student
    if student.user.firebase_token:
        logger.debug(f"sending fcm to {student.user}")
        FCM.notify(
            token=student.user.firebase_token,
            title="New Payment",
            body=f"{current_user.name} charged you for {amount}",
        )
    return {"data": payment.to_dict()}, 201


@teacher_routes.route("/students", methods=["GET"])
@jsonify_response
@login_required
@teacher_required
@paginate
def students():
    """allow filtering by name / area of student, and sort by balance,
    lesson number"""
    try:
        query = current_user.teacher.students
        args = flask.request.args
        extra_filters = {User: {"name": like_filter, "area": like_filter}}
        return Student.filter_and_sort(
            args, query, extra_filters=extra_filters, with_pagination=True
        )
    except ValueError:
        raise RouteError("Wrong parameters passed.")


@teacher_routes.route("/edit_data", methods=["POST"])
@jsonify_response
@login_required
@teacher_required
def edit_data():
    post_data = flask.request.get_json()
    teacher = current_user.teacher
    fields = ("price", "lesson_duration")
    for field in fields:
        if post_data.get(field):
            setattr(teacher, field, post_data.get(field))

    teacher.save()
    return {"data": current_user.to_dict()}


@teacher_routes.route("/<int:teacher_id>/approve", methods=["GET"])
@jsonify_response
@login_required
def approve(teacher_id):
    if not current_user.is_admin:
        raise RouteError("Not authorized.", 401)
    teacher = Teacher.get_by_id(teacher_id)
    teacher.update(is_approved=True)
    return {"data": teacher.to_dict()}


@teacher_routes.route("/payments/<int:payment_id>/receipt", methods=["GET"])
@jsonify_response
@login_required
@teacher_required
def add_receipt(payment_id):
    payment = Payment.get_by_id(payment_id)
    if not payment or payment.teacher != current_user.teacher:
        raise RouteError("Payment not found.", 404)

    api_key = flask.current_app.config.get("RECEIPTS_API_KEY")
    payload = {
        "api_key": api_key,
        "developer_email": "ronalister@gmail.com",
        "created_by_api_key": api_key,
        "transaction_id": payment.id,
        "type": 320,
        "customer_name": payment.student.user.name,
        "customer_email": payment.student.user.email,
        "customer_crn": payment.crn,
        "item": {
            1: {
                "details": payment.details,
                "amount": "1",
                "price": payment.amount,
                "price_inc_vat": 1,  # this price include the VAT
            }
        },
        "payment": {
            1: {"payment_type": payment.payment_type.value, "payment": payment.amount}
        },
        "price_total": payment.amount,  # /*THIS IS A MUST ONLY IN INVOICE RECIEPT*/
    }

    resp = requests.post(RECEIPT_URL, json=payload)
    resp_json = resp.json()
    if resp_json["success"]:
        payment.update(pdf_link=resp_json["pdf_link"])
        return {"message": "Receipt added successfully."}

    raise RouteError(resp_json["errMsg"])
