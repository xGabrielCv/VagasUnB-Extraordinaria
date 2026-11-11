"""Independent versioned schema; immutable SQL for revision 0001."""

from alembic import op

revision = "0001"
down_revision = None

STATEMENTS = [
    "\nCREATE TABLE admin_sessions (\n\tdigest VARCHAR(64) NOT NULL, \n\tcsrf VARCHAR(64) NOT NULL, \n\texpires TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (digest)\n)\n\n",
    "\nCREATE TABLE admins (\n\tid INTEGER NOT NULL, \n\tpassword TEXT NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "\nCREATE TABLE classrooms (\n\tid VARCHAR(32) NOT NULL, \n\tcode VARCHAR(32) NOT NULL, \n\tname VARCHAR(250) NOT NULL, \n\tsearch TEXT NOT NULL, \n\tnumber VARCHAR(20) NOT NULL, \n\tperiod VARCHAR(20) NOT NULL, \n\tunits JSON NOT NULL, \n\tcampus VARCHAR(120), \n\tprovenance VARCHAR(200) NOT NULL, \n\tteachers TEXT, \n\tschedule TEXT, \n\tlocation TEXT, \n\toffered INTEGER, \n\toccupied INTEGER, \n\tavailable INTEGER, \n\tobserved TIMESTAMP WITH TIME ZONE, \n\tstate VARCHAR(20) NOT NULL, \n\tcandidate TIMESTAMP WITH TIME ZONE, \n\tcycle INTEGER NOT NULL, \n\tmissing INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (code, number, period)\n)\n\n",
    "CREATE INDEX ix_classrooms_search ON classrooms (search)",
    "CREATE INDEX ix_classroom_period_code ON classrooms (period, code)",
    "\nCREATE TABLE events (\n\tid VARCHAR(32) NOT NULL, \n\tcreated TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcategory VARCHAR(40) NOT NULL, \n\tcode VARCHAR(60) NOT NULL, \n\tvalues JSON NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_events_created ON events (created)",
    "\nCREATE TABLE recipients (\n\tid VARCHAR(32) NOT NULL, \n\tcipher TEXT NOT NULL, \n\tlookup VARCHAR(64) NOT NULL, \n\tpaused BOOLEAN NOT NULL, \n\tactive_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (lookup)\n)\n\n",
    "\nCREATE TABLE runs (\n\tid VARCHAR(32) NOT NULL, \n\tscope VARCHAR(100) NOT NULL, \n\tstarted TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished TIMESTAMP WITH TIME ZONE, \n\tduration_ms INTEGER NOT NULL, \n\tcount INTEGER NOT NULL, \n\tvalid BOOLEAN NOT NULL, \n\terror VARCHAR(40), \n\tPRIMARY KEY (id)\n)\n\n",
    "CREATE INDEX ix_runs_scope ON runs (scope)",
    "\nCREATE TABLE settings (\n\tkey VARCHAR(80) NOT NULL, \n\tvalue JSON NOT NULL, \n\tPRIMARY KEY (key)\n)\n\n",
    "\nCREATE TABLE callbacks (\n\ttoken VARCHAR(32) NOT NULL, \n\trecipient_id VARCHAR(32) NOT NULL, \n\taction VARCHAR(40) NOT NULL, \n\targs JSON NOT NULL, \n\texpires TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (token), \n\tFOREIGN KEY(recipient_id) REFERENCES recipients (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_callbacks_recipient_id ON callbacks (recipient_id)",
    "\nCREATE TABLE cooldowns (\n\trecipient_id VARCHAR(32) NOT NULL, \n\tclassroom_id VARCHAR(32) NOT NULL, \n\tuntil TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (recipient_id, classroom_id), \n\tFOREIGN KEY(recipient_id) REFERENCES recipients (id) ON DELETE CASCADE, \n\tFOREIGN KEY(classroom_id) REFERENCES classrooms (id) ON DELETE CASCADE\n)\n\n",
    "\nCREATE TABLE subscriptions (\n\tid VARCHAR(32) NOT NULL, \n\trecipient_id VARCHAR(32) NOT NULL, \n\tcode VARCHAR(32) NOT NULL, \n\tperiod VARCHAR(20) NOT NULL, \n\tnumbers JSON NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (recipient_id, code, period), \n\tFOREIGN KEY(recipient_id) REFERENCES recipients (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_subscriptions_recipient_id ON subscriptions (recipient_id)",
    "\nCREATE TABLE outbox (\n\tid VARCHAR(32) NOT NULL, \n\trecipient_id VARCHAR(32) NOT NULL, \n\tsubscription_id VARCHAR(32) NOT NULL, \n\tclassroom_id VARCHAR(32) NOT NULL, \n\tcycle INTEGER NOT NULL, \n\trevision INTEGER NOT NULL, \n\tmode VARCHAR(20) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tcreated TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdue TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlease TIMESTAMP WITH TIME ZONE, \n\tattempts INTEGER NOT NULL, \n\tsent TIMESTAMP WITH TIME ZONE, \n\terror VARCHAR(40), \n\tPRIMARY KEY (id), \n\tUNIQUE (recipient_id, classroom_id, cycle), \n\tFOREIGN KEY(recipient_id) REFERENCES recipients (id) ON DELETE CASCADE, \n\tFOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE, \n\tFOREIGN KEY(classroom_id) REFERENCES classrooms (id) ON DELETE CASCADE\n)\n\n",
    "CREATE INDEX ix_outbox_due ON outbox (status, due)",
    "CREATE INDEX ix_outbox_recipient_id ON outbox (recipient_id)",
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a private export")
