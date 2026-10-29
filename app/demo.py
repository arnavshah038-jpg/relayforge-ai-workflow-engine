DEMO_COMPANY = "Demo Corp"

# Public on purpose: these accounts only exist on a demo server and only see fake data.
DEMO_ACCOUNTS = [
    {"email": "agent@demo.test", "password": "demo-pass-123", "role": "agent",
     "label": "Log in as agent (sends tickets)"},
    {"email": "manager@demo.test", "password": "demo-pass-123", "role": "manager",
     "label": "Log in as manager (approves)"},
]

SAMPLE_TICKETS = [
    ("demo-seed-refund", "ravi@example.com", "Refund",
     "Charged twice, please refund Rs 2000 for order 1042"),
    ("demo-seed-password", "sita@example.com", "Forgot my password",
     "I cannot login to my account."),
]
