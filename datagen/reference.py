"""Fixed reference data and constants for synthetic data generation. All entities are fictional."""
from datetime import date

SEED = 20261022
AS_OF = date(2026, 10, 22)  # "today" for every generated record; mock services treat this as the current date
GST_PCT = 18

# Fictional External Benchmark Lending Rate history (effective date, rate %).
EBLR_HISTORY = [
    (date(2022, 4, 1), 7.75),
    (date(2022, 6, 15), 8.25),
    (date(2022, 9, 15), 8.75),
    (date(2023, 2, 15), 9.25),
    (date(2025, 2, 15), 9.00),
    (date(2025, 4, 15), 8.75),
    (date(2025, 6, 15), 8.25),
    (date(2026, 3, 15), 8.50),
    (date(2026, 9, 15), 9.00),
]

FAILURE_REASONS = {
    "INSUFFICIENT_FUNDS": "Balance insufficient in debit account",
    "MANDATE_INACTIVE": "NACH mandate not active at destination bank",
    "ACCOUNT_DORMANT": "Debit account dormant or frozen",
    "BANK_TECHNICAL_ERROR": "Technical failure at customer's bank",
    "NPCI_SYSTEM_ERROR": "Clearing network (NPCI) system error",
}
TECHNICAL_FAILURES = {"BANK_TECHNICAL_ERROR", "NPCI_SYSTEM_ERROR"}  # see policy LP-CHG-002
FAILURE_WEIGHTS = {
    "INSUFFICIENT_FUNDS": 70, "MANDATE_INACTIVE": 8, "ACCOUNT_DORMANT": 5,
    "BANK_TECHNICAL_ERROR": 12, "NPCI_SYSTEM_ERROR": 5,
}

RISK_PROFILES = {  # share of customers, per-instalment failure probability
    "GOOD": (70, 0.01),
    "AVERAGE": (22, 0.05),
    "STRESSED": (8, 0.18),
}

BANKS = ["HDFC Bank", "ICICI Bank", "State Bank of India", "Axis Bank", "Kotak Mahindra Bank", "Bank of Baroda", "Canara Bank", "IDFC FIRST Bank"]

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna", "Ishaan", "Rohan",
    "Kabir", "Siddharth", "Rahul", "Karan", "Nikhil", "Manish", "Suresh", "Ramesh", "Imran", "Farhan",
    "Ananya", "Diya", "Aadhya", "Saanvi", "Kavya", "Priya", "Neha", "Pooja", "Sneha", "Meera",
    "Lakshmi", "Divya", "Shreya", "Riya", "Anjali", "Nandini", "Fatima", "Zoya", "Harpreet", "Simran",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Mehta", "Iyer", "Nair", "Reddy", "Rao", "Patel", "Shah",
    "Joshi", "Kulkarni", "Deshpande", "Banerjee", "Chatterjee", "Das", "Bose", "Singh", "Kaur", "Gill",
    "Khan", "Qureshi", "Menon", "Pillai", "Agarwal", "Jain", "Malhotra", "Kapoor", "Saxena", "Mishra",
]
CITIES = [  # city, state, pincode prefix
    ("Delhi", "Delhi", "110"), ("Gurugram", "Haryana", "122"), ("Noida", "Uttar Pradesh", "201"),
    ("Chandigarh", "Chandigarh", "160"), ("Jaipur", "Rajasthan", "302"), ("Lucknow", "Uttar Pradesh", "226"),
    ("Ludhiana", "Punjab", "141"), ("Dehradun", "Uttarakhand", "248"), ("Mumbai", "Maharashtra", "400"),
    ("Pune", "Maharashtra", "411"), ("Bengaluru", "Karnataka", "560"), ("Chennai", "Tamil Nadu", "600"),
    ("Hyderabad", "Telangana", "500"), ("Kolkata", "West Bengal", "700"), ("Ahmedabad", "Gujarat", "380"),
]
LANGUAGES = {"Delhi": "hi", "Gurugram": "hi", "Noida": "hi", "Chandigarh": "hi", "Jaipur": "hi", "Lucknow": "hi",
             "Ludhiana": "pa", "Dehradun": "hi", "Mumbai": "mr", "Pune": "mr", "Bengaluru": "kn",
             "Chennai": "ta", "Hyderabad": "te", "Kolkata": "bn", "Ahmedabad": "gu"}

MERCHANTS = [  # name, category, min, max amount
    ("BigBasket", "GROCERY", 400, 4500), ("Zepto", "GROCERY", 150, 1800), ("Swiggy", "FOOD_DELIVERY", 180, 1500),
    ("Zomato", "FOOD_DELIVERY", 200, 1600), ("Amazon", "ONLINE_SHOPPING", 300, 15000), ("Flipkart", "ONLINE_SHOPPING", 300, 18000),
    ("Myntra", "APPAREL", 600, 6000), ("IndianOil Fuel", "FUEL", 500, 4000), ("HP Petrol Pump", "FUEL", 500, 3500),
    ("Apollo Pharmacy", "HEALTH", 200, 3000), ("Uber", "TRAVEL", 120, 900), ("IndiGo", "AIRLINE", 3500, 16000),
    ("MakeMyTrip", "TRAVEL", 2500, 25000), ("PVR Cinemas", "ENTERTAINMENT", 400, 1800), ("Airtel Postpaid", "UTILITIES", 499, 1499),
    ("Tata Power", "UTILITIES", 900, 5500), ("Croma", "ELECTRONICS", 2000, 45000), ("Decathlon", "SPORTS", 800, 7000),
]
