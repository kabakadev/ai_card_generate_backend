from config import db

class UsageLimits(db.Model):
    __tablename__ = "usage_limits"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    month_key = db.Column(db.String(7), nullable=False, index=True)  # 'YYYY-MM'
    ai_prompt_count = db.Column(db.Integer, nullable=False, server_default="0")
    free_quota = db.Column(db.Integer, nullable=False, server_default="5")

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now(), onupdate=db.func.now())

    user = db.relationship("User", backref=db.backref("usage_limits", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint("user_id", "month_key", name="uq_usage_user_month"),
    )
