from flask import Flask
from flask_restful import Api, Resource

from config import Config
from app.controller.user import user_route
from app.controller.xas import report_route
from db.interface import db


DEFAULT_MODULES = [
    user_route,
    report_route
]

def create_app(config_class=Config):
    app = Flask(__name__)

    app.config.from_object(config_class)

    # initialize 
    db.init_app(app)

    # register 
    for module in DEFAULT_MODULES:
        app.register_blueprint(module)

    return app
