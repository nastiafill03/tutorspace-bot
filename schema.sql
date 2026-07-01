DROP TABLE IF EXISTS waitlist_users;

CREATE TABLE waitlist_users (
    telegram_id    bigint PRIMARY KEY,
    username       text,
    first_name     text,
    referred_by    bigint REFERENCES waitlist_users(telegram_id) ON DELETE SET NULL,
    referral_count int NOT NULL DEFAULT 0,
    months_earned  int NOT NULL DEFAULT 2,
    is_founder     boolean NOT NULL DEFAULT true,
    founder_number int,
    joined_at      timestamptz NOT NULL DEFAULT now()
);
