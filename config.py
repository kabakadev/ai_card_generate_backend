from flask import Flask
from flask_bcrypt import Bcrypt
from flask_migrate import Migrate
from flask_restful import Api
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from dotenv import load_dotenv
import os
from datetime import timedelta
from flask_cors import CORS

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config["JWT_TOKEN_LOCATION"] = ["headers"]      # ensure we read from Authorization header
app.config["JWT_HEADER_TYPE"] = "Bearer"            # default, but make explicit
app.config["JWT_DECODE_LEEWAY"] = 60   

app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=48)  # Extend to 48 hours

# Load database URI from .env
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'supersecretkey')

jwt = JWTManager(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)
bcrypt = Bcrypt(app)
api = Api(app)




app.config["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")

FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # add your deployed frontend here, e.g.:
    # "https://flashlearn-frontend.onrender.com",
]

CORS(
    app,
    resources={r"/*": {"origins": FRONTEND_ORIGINS}},
    supports_credentials=True,  # okay to keep on even if you use Authorization header
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
