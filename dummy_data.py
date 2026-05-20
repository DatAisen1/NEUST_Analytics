
import random
from pathlib import Path
import pandas as pd
from faker import Faker

fake = Faker()

# ============================================================
# CONFIG
# ============================================================
ROWS = 1000
OUTPUT_DIR = Path("data/raw")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ACADEMIC_YEARS = ["2023-2024"]
SEMESTERS = [1, 2]
GENDERS = ["Male", "Female", "Other", "Not Specified"]
YEAR_LEVELS = [1, 2, 3, 4, 5]

# ============================================================
# OFFICIAL NEUST SUMACAB CAMPUS PROGRAMS
# ============================================================
PROGRAMS = {
    "College of Architecture": {
        "Bachelor of Science in Architecture": []
    },

    "College of Criminology": {
        "Bachelor of Science in Criminology": []
    },

    "College of Education": {
        "Bachelor of Elementary Education (BEEd)": [],
        "Bachelor of Secondary Education (BSEd)": [
            "Science Education",
            "Mathematics Education",
            "English Education",
            "Filipino Education",
            "Social Studies Education"
        ],
        "Bachelor of Technology and Livelihood Education (BTLEd)": [],
        "Bachelor of Science in Industrial Education (BSIEd)": [],
        "Bachelor of Physical Education (BPEd)": [],
        "Bachelor of Early Childhood Education (BECEd)": [],
        "Bachelor of Special Needs Education (BSNEd)": [
            "Generalist",
            "Early Childhood Education"
        ]
    },

    "College of Engineering": {
        "Bachelor of Science in Civil Engineering": [],
        "Bachelor of Science in Electrical Engineering": [],
        "Bachelor of Science in Mechanical Engineering": []
    },

    "College of Information and Communication Technology": {
        "Bachelor of Science in Information Technology": [
            "Database Systems Technology",
            "Network Systems Technology",
            "Web Systems Technology"
        ]
    },

    "College of Management and Business Technology": {
        "Bachelor of Science in Business Administration": [
            "Financial Management",
            "Human Resource Development Management",
            "Marketing Management"
        ],
        "Bachelor of Science in Entrepreneurship": [],
        "Bachelor of Science in Hospitality Management": [],
        "Bachelor of Science in Hotel and Restaurant Management": [],
        "Bachelor of Science in Tourism Management": []
    },

    "College of Public Administration and Disaster Management": {
        "Bachelor of Public Administration": [],
        "Bachelor of Public Administration Major in Disaster Management": []
    }
}

# ============================================================
# FLATTEN PROGRAM STRUCTURE
# ============================================================
flat_programs = []
for college, programs in PROGRAMS.items():
    for program, majors in programs.items():
        if majors:
            for m in majors:
                flat_programs.append((college, program, m))
        else:
            flat_programs.append((college, program, "General"))

# ============================================================
# ENROLLMENT FLOW DATA
# ============================================================
enrollment_data = []

for _ in range(ROWS):
    college, program, major = random.choice(flat_programs)

    applicants = random.randint(50, 500)
    accepted = random.randint(30, applicants)
    enrolled = random.randint(20, accepted)

    new_students = random.randint(0, enrolled)
    remaining = enrolled - new_students

    transferees = random.randint(0, remaining)
    returnees = remaining - transferees

    enrollment_data.append({
        "Academic Year": random.choice(ACADEMIC_YEARS),
        "Semester": random.choice(SEMESTERS),
        "College/Department": college,
        "Program/Course": program,
        "Major": major,
        "Year Level": random.choice(YEAR_LEVELS),
        "Gender": random.choice(GENDERS),
        "Applicants": applicants,
        "Accepted Applicants": accepted,
        "Total Enrolled": enrolled,
        "New Students": new_students,
        "Transferees": transferees,
        "Returnees": returnees,
    })

# ============================================================
# STUDENT OUTCOMES DATA
# ============================================================
outcomes_data = []

for _ in range(ROWS):
    college, program, major = random.choice(flat_programs)

    outcomes_data.append({
        "Academic Year": random.choice(ACADEMIC_YEARS),
        "Semester": random.choice(SEMESTERS),
        "College/Department": college,
        "Program/Course": program,
        "Major": major,
        "Year Level": random.choice(YEAR_LEVELS),
        "Gender": random.choice(GENDERS),
        "Graduates": random.randint(0, 200),
        "Dropouts": random.randint(0, 50),
        "Shifters Out": random.randint(0, 30),
        "Shifters In": random.randint(0, 30),
    })

# ============================================================
# SAVE FILE
# ============================================================

output_file = OUTPUT_DIR / "neust_dummy_data_AY2024.xlsx"
with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    pd.DataFrame(enrollment_data).to_excel(
        writer,
        sheet_name="Enrollment_Flow",
        index=False,
    )
    pd.DataFrame(outcomes_data).to_excel(
        writer,
        sheet_name="Student_Outcomes",
        index=False,
    )

print("=" * 60)
print("NEUST Sumacab Dummy Data Generated")
print("Rows per sheet:", ROWS)
print("Output file:", output_file)
print("=" * 60)
