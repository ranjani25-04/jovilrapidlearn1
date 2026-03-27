from flask import Flask, render_template, request, redirect, session, send_file, abort
import sqlite3
import re
import io
from datetime import datetime
from urllib.parse import quote
import html as html_lib
import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors

app = Flask(__name__)
app.secret_key = "lmskey"

# DATABASE
conn = sqlite3.connect("lms.db", check_same_thread=False)
cursor = conn.cursor()

# USERS TABLE
cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
id INTEGER PRIMARY KEY AUTOINCREMENT,
name TEXT,
email TEXT,
password TEXT
)
""")

# COURSES TABLE
cursor.execute("""
CREATE TABLE IF NOT EXISTS courses(
id INTEGER PRIMARY KEY AUTOINCREMENT,
course TEXT,
description TEXT
)
""")

# LESSONS TABLE (with optional video url)
cursor.execute("""
CREATE TABLE IF NOT EXISTS lessons(
id INTEGER PRIMARY KEY AUTOINCREMENT,
course_id INTEGER,
title TEXT,
content TEXT,
video_url TEXT
,
audio_url TEXT
)
""")
try:
    cursor.execute("ALTER TABLE lessons ADD COLUMN video_url TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE lessons ADD COLUMN audio_url TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

# QUIZ TABLE
cursor.execute("""
CREATE TABLE IF NOT EXISTS quiz(
id INTEGER PRIMARY KEY AUTOINCREMENT,
lesson_id INTEGER,
question TEXT,
option1 TEXT,
option2 TEXT,
option3 TEXT,
option4 TEXT,
answer TEXT
)
""")

# MULTI-QUESTION QUIZ TABLE (5 questions per lesson)
cursor.execute("""
CREATE TABLE IF NOT EXISTS quiz_questions(
id INTEGER PRIMARY KEY AUTOINCREMENT,
lesson_id INTEGER,
question TEXT,
option1 TEXT,
option2 TEXT,
option3 TEXT,
option4 TEXT,
answer TEXT
)
""")

# ENROLLMENTS TABLE
cursor.execute("""
CREATE TABLE IF NOT EXISTS enrollments(
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
course_id INTEGER,
enrolled_at TEXT,
UNIQUE(user_id, course_id)
)
""")

# LESSON PROGRESS TABLE
cursor.execute("""
CREATE TABLE IF NOT EXISTS lesson_progress(
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
lesson_id INTEGER,
completed_at TEXT,
UNIQUE(user_id, lesson_id)
)
""")

# FINAL COURSE ASSESSMENTS (Final Quiz)
cursor.execute("""
CREATE TABLE IF NOT EXISTS course_assessments(
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
course_id INTEGER,
score INTEGER,
total INTEGER,
percentage INTEGER,
passed INTEGER,
attempted_at TEXT
)
""")

# PER-LESSON QUIZ ATTEMPTS (for tracking quiz completion + scores)
cursor.execute("""
CREATE TABLE IF NOT EXISTS quiz_attempts(
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER,
lesson_id INTEGER,
score INTEGER,
total INTEGER,
percentage INTEGER,
attempted_at TEXT,
UNIQUE(user_id, lesson_id)
)
""")

conn.commit()

PASS_PERCENT = 60


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def require_login():
    return "user_id" in session


def get_video_embed_url(video_url):
    if not video_url or not isinstance(video_url, str):
        return None
    url = video_url.strip()
    if not url:
        return None
    m = re.search(r"(?:youtube\\.com/watch\\?v=|youtu\\.be/|youtube\\.com/embed/)([a-zA-Z0-9_-]{11})", url)
    if m:
        return "https://www.youtube.com/embed/" + m.group(1)
    m = re.search(r"vimeo\\.com/(?:video/)?(\\d+)", url)
    if m:
        return "https://player.vimeo.com/video/" + m.group(1)
    return None


def normalize_quiz_value(value) -> str:
    """
    Normalize quiz option values coming from HTML form submissions.
    Jinja/HTML escaping can turn values like "<h1>" into "&lt;h1&gt;" in the page,
    so we decode entities before comparing.
    """
    return html_lib.unescape(str(value or "")).strip().lower()


def generate_simple_certificate_pdf_bytes(student_name: str, course_name: str) -> bytes:
    """
    Certificate generator.

    If you export your Canva design as an image and save it as:
      `certificate_template.png` (in the same folder as `app.py`)
    this function will use it as the PDF background and place text over it.
    """

    def safe_text(s: str) -> str:
        s = str(s or "").replace("\n", " ").strip()
        # Helvetica (Type 1) doesn't support full Unicode reliably.
        # Keep the certificate readable by falling back to ASCII if needed.
        try:
            s.encode("latin-1")
            return s
        except UnicodeEncodeError:
            return s.encode("ascii", "ignore").decode("ascii")

    issued = datetime.utcnow().strftime("%Y-%m-%d")

    student_name = safe_text(student_name)
    course_name = safe_text(course_name)

    buf = io.BytesIO()
    width, height = letter
    c = canvas.Canvas(buf, pagesize=letter)

    # Optional design background from Canva export (PNG/JPG).
    template_path_png = os.path.join(os.path.dirname(__file__), "certificate_template.png")
    template_path_jpg = os.path.join(os.path.dirname(__file__), "certificate_template.jpg")
    template_path = template_path_png if os.path.exists(template_path_png) else (
        template_path_jpg if os.path.exists(template_path_jpg) else None
    )

    if template_path:
        img = ImageReader(template_path)
        # Stretch to page. If you export in a different aspect ratio,
        # you can adjust coordinates/scale later.
        c.drawImage(img, 0, 0, width=width, height=height, mask='auto')
    else:
        # Fallback: simple border + white background.
        c.setFillColor(colors.white)
        c.rect(0, 0, width, height, fill=1, stroke=0)
        c.setStrokeColor(colors.HexColor("#0a2540"))
        c.setLineWidth(2)
        margin = 36
        c.rect(margin, margin, width - 2 * margin, height - 2 * margin, fill=0, stroke=1)

    c.setFillColor(colors.HexColor("#2f241a"))

    # Canva-style placement: keep existing template heading, overlay only dynamic fields.
    c.setFont("Helvetica-BoldOblique", 34)
    c.drawCentredString(width / 2, height * 0.555, student_name)

    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height * 0.41, f"Successfully completed: {course_name}")

    c.setFont("Helvetica", 12)
    c.drawString(60, 58, f"Issued on: {issued}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def seed_default_data():
    def make_detailed_content(lesson_title: str, base_content: str) -> str:
        base = (base_content or "").strip()
        # Keep everything in one paragraph (templates render it inside a <p>).
        if lesson_title == "HTML Tutorial":
            extra = (
                " Start from the basics: learn how HTML uses elements to structure headings, paragraphs, links, and images. "
                "You will also learn the key tags behind common questions like <h1> for the largest heading, <img> for images, and the link attribute href. "
                "Next, move toward better practices by using semantic HTML, so your content is easier to understand and more accessible. "
                "By the end, you should be able to explain not only what HTML is, but also why meaning-focused structure helps both users and search engines."
            )
        elif lesson_title == "CSS Tutorial":
            extra = (
                " Begin with the basics of CSS: learn that CSS controls presentation and layout for your HTML. "
                "Then cover selectors and styles you will use often: targeting a class with .class and changing text color with the color property. "
                "You will also connect the box model idea to real spacing using margin (space outside the border) and practice responsive-friendly styling. "
                "Finally, build toward intermediate layouts by understanding when Flexbox is best for 1D alignment."
            )
        elif lesson_title == "JavaScript Tutorial":
            extra = (
                " Start with JavaScript basics: learn that JavaScript is used to add interactivity to web pages. "
                "You will practice event-driven thinking by recognizing the click event for buttons. "
                "Next, learn important syntax for modern code: use let for block-scoped variables. "
                "You will also understand the DOM (Document Object Model) as the place where scripts read and update page structure. "
                "Finally, see how JSON represents structured data, which is essential for building real applications."
            )
        elif lesson_title == "Responsive Web Design":
            extra = (
                " Begin with responsive fundamentals: understand that responsive design adapts layouts to different screen sizes. "
                "Learn the core techniques like media queries for breakpoints and rem for scaling based on root font size. "
                "Then apply the mobile-first mindset (start by designing for small screens). "
                "Finally, build toward stronger layouts by choosing the right tools for columns, such as Grid or Flexbox."
            )
        elif lesson_title == "Mini Project: Landing Page":
            extra = (
                " Start from the basics of a landing page: combine HTML structure with CSS styling and a small amount of JavaScript enhancement. "
                "Learn practical content and usability rules like using a clear and visible CTA button. "
                "Improve performance by choosing optimized images, and improve navigation with a consistent layout and clear headings. "
                "To finish the project, deploy your static site using GitHub Pages so learners can share and demonstrate their work."
            )
        else:
            extra = (
                " In this lesson, you will move from basic understanding to advanced confidence step-by-step. "
                "You will first learn the core idea and how it is used in real situations, then build intermediate understanding using common patterns. "
                "Finally, you will focus on applying the concept in a way that prepares you for quizzes and practical tasks."
            )
        return (base + " " + extra).strip()

    default_courses = [
        ("Web Development", "Learn HTML, CSS, and JavaScript step-by-step and build responsive, interactive websites."),
        ("Python Programming", "Learn Python from basics to real programs: syntax, control flow, data structures, and OOP."),
        ("Java Programming", "Learn Java fundamentals: syntax, OOP, collections, exceptions, and basic problem solving."),
        ("Data Science Basics", "Learn how data is cleaned, analyzed, visualized, and used for simple models."),
        ("SQL & Databases", "Learn SQL queries, joins, grouping, and practical database design basics."),
    ]

    # Video defaults for courses that currently have empty video_url.
    course_video_defaults = {
        2: "https://www.youtube.com/watch?v=eWRfhZUzrAc",   # Python (full course)
        3: "https://www.youtube.com/watch?v=grEKMHGYyns",   # Java (full course)
        4: "https://www.youtube.com/watch?v=CMEWVn1uZpQ",   # Data Science (full course)
        5: "https://www.youtube.com/watch?v=7S_tz1z_5bA",    # SQL (full course)
    }

    # Ensure courses exist (do not duplicate).
    for name, desc in default_courses:
        cursor.execute("SELECT id FROM courses WHERE course=?", (name,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO courses(course,description) VALUES(?,?)", (name, desc))
    conn.commit()

    default_lessons = [
        # Course 1: Web Development (exactly 5 lessons; each lesson will have 5 quiz questions)
        (1, "HTML Tutorial", "HTML (HyperText Markup Language) is the foundational language of the web used to structure content using elements like headings, paragraphs, links, images, and sections so a browser can render a page correctly.", "https://www.youtube.com/watch?v=pQN-pnXPaVg", ""),
        (1, "CSS Tutorial", "CSS (Cascading Style Sheets) describes how HTML should look—colors, fonts, spacing, and layout—so you can build clean, readable, and responsive pages.", "https://www.youtube.com/watch?v=1Rs2ND1ryYc", ""),
        (1, "JavaScript Tutorial", "JavaScript is the browser’s programming language used to add interactivity, handle events, validate forms, and update the page dynamically through the DOM.", "https://www.youtube.com/watch?v=W6NZfCO5SIk", ""),
        (1, "Responsive Web Design", "Responsive design is the practice of building layouts that adapt to different screen sizes using flexible units, breakpoints (media queries), and modern layout tools so your site works well on mobile and desktop.", "https://www.youtube.com/watch?v=srvUrASNj0s", ""),
        (1, "Mini Project: Landing Page", "A landing page project combines HTML structure, CSS styling, and JavaScript enhancements to create a complete, shareable page that looks professional and works smoothly.", "https://www.youtube.com/watch?v=G3e-cpL7ofc", ""),

        # Course 2: Python Programming
        (2, "Python Introduction", "Python is a high-level, beginner-friendly programming language known for readable syntax and broad use in web apps, automation, data analysis, and scripting.", "", ""),
        (2, "Python Variables", "Variables in Python store values without explicit type declarations—Python infers the type based on the value you assign to the name.", "", ""),
        (2, "Python Data Types", "Common Python data types include int, float, str, bool, list, tuple, set, and dict—each type supports different operations and use cases.", "", ""),
        (2, "Python Operators", "Operators are symbols for actions like math (+, -, *, /), comparisons (==, <, >), and logic (and/or/not) to build expressions and decisions.", "", ""),
        (2, "Python if-else", "if/elif/else statements allow your program to run different code depending on conditions, which is essential for decision making.", "", ""),
        (2, "Python Loops", "Loops (for and while) repeat code blocks to process collections and automate repeated tasks, with break and continue controlling flow.", "", ""),
        (2, "Python Functions", "Functions group reusable logic into named blocks that can accept inputs (parameters) and return outputs, helping you write clean code.", "", ""),
        (2, "Python Lists", "Lists are ordered, mutable collections used to store multiple items in one variable and support operations like append, remove, and slicing.", "", ""),
        (2, "Python Dictionaries", "Dictionaries store key-value pairs for fast lookup, making them ideal for mapping IDs to values and representing structured data.", "", ""),
        (2, "Python File Handling", "File handling lets programs read and write files using open() and context managers, which is essential for logs, configs, and data storage.", "", ""),
        (2, "Python OOP Basics", "OOP in Python uses classes and objects to model real-world entities; classes define attributes and methods to create reusable structures.", "", ""),

        # Course 3: Java Programming
        (3, "Java Introduction", "Java is a statically typed, object-oriented language that runs on the JVM, allowing code to run on many platforms with strong tooling support.", "", ""),
        (3, "Java Variables & Data Types", "Java variables must be declared with types (int, double, boolean, char, String), which helps catch errors early and improves clarity.", "", ""),
        (3, "Java Operators", "Java operators perform arithmetic, comparisons, and logic, enabling calculations and conditional checks inside programs.", "", ""),
        (3, "Java Control Statements", "Control statements like if/else, switch, for, while, and do-while manage program flow, letting you branch and repeat operations.", "", ""),
        (3, "Java Arrays", "Arrays store multiple values of the same type in a fixed-size structure and are often used for loops and algorithm practice.", "", ""),
        (3, "Java Classes & Objects", "Classes define blueprints for objects, and objects are instances that store state and behavior—this is the core of Java development.", "", ""),
        (3, "Java Inheritance", "Inheritance allows one class to reuse and extend another, enabling code reuse and polymorphism in larger applications.", "", ""),
        (3, "Java Exceptions", "Exceptions are runtime errors that can be handled using try/catch to prevent program crashes and create safer applications.", "", ""),
        (3, "Java Collections (Intro)", "Collections like List, Set, and Map store groups of objects with flexible sizing and powerful operations compared to arrays.", "", ""),

        # Course 4: Data Science Basics
        (4, "What is Data Science?", "Data science combines statistics, programming, and domain knowledge to extract insights from data, often using analysis, visualization, and modeling.", "", ""),
        (4, "Data Types in Data", "Data can be numerical, categorical, text, or time-based; understanding types helps you choose the right cleaning and analysis methods.", "", ""),
        (4, "Data Cleaning Basics", "Data cleaning removes errors like missing values, duplicates, and inconsistent formats so analysis and models are more reliable.", "", ""),
        (4, "Exploratory Data Analysis (EDA)", "EDA is the process of summarizing and visualizing data to understand patterns, outliers, and relationships before building models.", "", ""),
        (4, "Visualization Basics", "Visualization turns data into charts (bar, line, histogram, scatter) so humans can quickly understand trends and comparisons.", "", ""),
        (4, "Intro to Machine Learning", "Machine learning trains algorithms on data to make predictions or decisions, using steps like training/testing splits and evaluation metrics.", "", ""),

        # Course 5: SQL & Databases
        (5, "What is a Database?", "A database is an organized system for storing and retrieving data; relational databases use tables and relationships to keep data structured.", "", ""),
        (5, "SQL SELECT", "SELECT is used to read data from tables; you can choose columns, filter rows, and sort results to answer questions from data.", "", ""),
        (5, "SQL WHERE", "WHERE filters rows based on conditions, helping you retrieve only the records that match specific criteria.", "", ""),
        (5, "SQL ORDER BY", "ORDER BY sorts results by one or more columns, which is useful for ranking, latest-first lists, and clean reporting.", "", ""),
        (5, "SQL Aggregations", "Aggregations like COUNT, SUM, AVG, MIN, and MAX summarize data, often combined with GROUP BY for grouped reports.", "", ""),
        (5, "SQL GROUP BY & HAVING", "GROUP BY creates groups of rows for aggregation, while HAVING filters groups after aggregation, enabling powerful summaries.", "", ""),
        (5, "SQL JOINs", "JOINs combine rows from multiple tables based on related keys, allowing you to query connected data like orders with customers.", "", ""),
        (5, "SQL INSERT/UPDATE/DELETE", "INSERT adds rows, UPDATE modifies existing rows, and DELETE removes rows—these are the basic commands for changing data.", "", ""),
        (5, "Database Design Basics", "Good design uses primary keys, foreign keys, and normalization to reduce duplication and keep data consistent and scalable.", "", ""),
    ]

    # Upsert lessons + ensure quizzes exist (without duplicating).
    for cid, title, base_content, video_url, audio_url in default_lessons:
        desired_content = make_detailed_content(title, base_content)
        desired_video_url = (video_url or "").strip()
        if not desired_video_url:
            desired_video_url = course_video_defaults.get(cid, "")

        cursor.execute("SELECT id FROM lessons WHERE course_id=? AND title=?", (cid, title))
        existing = cursor.fetchone()
        if existing:
            lesson_id = existing[0]
            cursor.execute(
                "UPDATE lessons SET content=?, video_url=?, audio_url=? WHERE id=?",
                (desired_content, desired_video_url, audio_url or "", lesson_id),
            )
        else:
            cursor.execute(
                "INSERT INTO lessons(course_id,title,content,video_url,audio_url) VALUES(?,?,?,?,?)",
                (cid, title, desired_content, desired_video_url, audio_url or ""),
            )
            lesson_id = cursor.lastrowid

        # Seed quiz questions only if missing for this lesson.
        cursor.execute("SELECT COUNT(*) FROM quiz_questions WHERE lesson_id=?", (lesson_id,))
        if cursor.fetchone()[0] > 0:
            continue

        if cid == 1:
            if title == "HTML Tutorial":
                questions = [
                    ("What is HTML mainly used for?", "Structuring web pages", "Styling web pages", "Querying databases", "Running servers", "Structuring web pages"),
                    ("Which tag is commonly used for the largest heading?", "<h1>", "<p>", "<div>", "<span>", "<h1>"),
                    ("Which attribute is required for a link destination?", "href", "src", "alt", "type", "href"),
                    ("Which tag is used to display an image?", "<img>", "<image>", "<pic>", "<photo>", "<img>"),
                    ("Why is semantic HTML useful?", "Improves meaning and accessibility", "Makes pages heavier", "Removes CSS need", "Replaces JavaScript", "Improves meaning and accessibility"),
                ]
            elif title == "CSS Tutorial":
                questions = [
                    ("What does CSS control?", "Presentation and layout", "Database tables", "Server routing", "File compression", "Presentation and layout"),
                    ("Which selector targets a class?", ".class", "#id", "tag()", "@class", ".class"),
                    ("Which property changes text color?", "color", "font", "paint", "text-color", "color"),
                    ("Which box model part creates space outside border?", "margin", "padding", "content", "outline", "margin"),
                    ("Which layout is best for 1D alignment?", "Flexbox", "Grid only", "Tables", "Frames", "Flexbox"),
                ]
            elif title == "JavaScript Tutorial":
                questions = [
                    ("JavaScript is mainly used to:", "Add interactivity", "Replace HTML", "Store images", "Create databases", "Add interactivity"),
                    ("Which keyword declares a block-scoped variable?", "let", "var", "define", "int", "let"),
                    ("DOM stands for:", "Document Object Model", "Data Output Method", "Design Object Map", "Document Order Markup", "Document Object Model"),
                    ("Which event happens when a button is clicked?", "click", "hover", "load", "submit", "click"),
                    ("What does JSON usually represent?", "Structured data format", "Image type", "CSS property", "Audio codec", "Structured data format"),
                ]
            elif title == "Responsive Web Design":
                questions = [
                    ("Responsive design is about:", "Adapting to screen sizes", "Adding animations only", "Storing data", "Compiling code", "Adapting to screen sizes"),
                    ("Which CSS feature defines breakpoints?", "media queries", "variables", "imports", "mixins", "media queries"),
                    ("Which unit scales with root font size?", "rem", "px", "pt", "cm", "rem"),
                    ("Mobile-first means:", "Start design for small screens first", "Start from desktop only", "Avoid CSS", "Use only tables", "Start design for small screens first"),
                    ("Which layout helps responsive columns?", "Grid or Flexbox", "Frameset", "Marquee", "Blink", "Grid or Flexbox"),
                ]
            else:
                questions = [
                    ("A landing page is:", "A focused page for a goal", "A database", "A programming language", "A browser extension", "A focused page for a goal"),
                    ("Best practice for CTA button is:", "Clear and visible", "Hidden in footer", "Same as background", "No text", "Clear and visible"),
                    ("Which improves page performance?", "Optimized images", "Huge videos autoplay", "Many fonts", "Uncompressed files", "Optimized images"),
                    ("Which helps user navigation?", "Consistent layout", "Random colors", "No headings", "Broken links", "Consistent layout"),
                    ("Deploying a static site can be done with:", "GitHub Pages", "Only paid servers", "Printer drivers", "BIOS update", "GitHub Pages"),
                ]

            for q, o1, o2, o3, o4, ans in questions:
                cursor.execute(
                    "INSERT INTO quiz_questions(lesson_id,question,option1,option2,option3,option4,answer) VALUES(?,?,?,?,?,?,?)",
                    (lesson_id, q, o1, o2, o3, o4, ans),
                )
        else:
            cursor.execute(
                "INSERT INTO quiz_questions(lesson_id,question,option1,option2,option3,option4,answer) VALUES(?,?,?,?,?,?,?)",
                (lesson_id, f"What is the main idea of {title}?", "Learning the concept", "Installing drivers", "Building hardware", "Formatting disks", "Learning the concept"),
            )

    conn.commit()


seed_default_data()


# HOME
@app.route("/")
def home():
    return render_template("home.html")


# LOGIN PAGE
@app.route("/login")
def login():
    return render_template("login.html")


# REGISTER
@app.route("/register", methods=["POST"])
def register():
    name = request.form["name"]
    email = request.form["email"]
    password = request.form["password"]
    cursor.execute("INSERT INTO users(name,email,password) VALUES(?,?,?)", (name, email, password))
    conn.commit()
    return redirect("/login")


# LOGIN CHECK
@app.route("/logincheck", methods=["POST"])
def logincheck():
    email = request.form["email"]
    password = request.form["password"]

    # ADMIN LOGIN
    if email == "admin@gmail.com" and password == "admin123":
        return redirect("/admin")

    cursor.execute("SELECT * FROM users WHERE email=? AND password=?", (email, password))
    user = cursor.fetchone()

    if user:
        session["user_id"] = user[0]
        session["user_name"] = user[1]
        session["user_email"] = user[2]
        return redirect("/courses")
    return redirect("/login?error=invalid")


# STUDENT PORTAL
@app.route("/courses")
def courses():
    if not require_login():
        return redirect("/login")

    view = request.args.get("view")
    rid = request.args.get("id")

    cursor.execute("SELECT * FROM courses")
    courses = cursor.fetchall()

    cursor.execute(
        """SELECT c.* FROM courses c
           JOIN enrollments e ON e.course_id=c.id
           WHERE e.user_id=? ORDER BY e.enrolled_at DESC""",
        (session["user_id"],),
    )
    my_courses = cursor.fetchall()

    # counts
    cursor.execute("SELECT course_id, COUNT(*) FROM lessons GROUP BY course_id")
    lesson_counts = {str(r[0]): r[1] for r in cursor.fetchall()}

    # progress
    cursor.execute("SELECT lesson_id FROM lesson_progress WHERE user_id=?", (session["user_id"],))
    completed_lessons = {str(r[0]) for r in cursor.fetchall()}

    cursor.execute("SELECT lesson_id FROM quiz_attempts WHERE user_id=?", (session["user_id"],))
    completed_quizzes = {str(r[0]) for r in cursor.fetchall()}

    cursor.execute("""
        SELECT l.course_id,
               COUNT(l.id) AS total_lessons,
               SUM(CASE WHEN lp.id IS NOT NULL THEN 1 ELSE 0 END) AS completed_lessons
        FROM lessons l
        LEFT JOIN lesson_progress lp ON lp.lesson_id=l.id AND lp.user_id=?
        GROUP BY l.course_id
    """, (session["user_id"],))
    course_progress = {str(r[0]): {"total": r[1], "completed": r[2] or 0} for r in cursor.fetchall()}

    # Latest final assessment per course (if any)
    cursor.execute("""
        SELECT course_id, score, total, percentage, passed, attempted_at
        FROM course_assessments
        WHERE user_id=?
        ORDER BY attempted_at DESC, id DESC
    """, (session["user_id"],))
    assessments = {}
    for r in cursor.fetchall():
        cid = str(r[0])
        if cid not in assessments:
            assessments[cid] = {
                "score": r[1],
                "total": r[2],
                "percentage": r[3],
                "passed": bool(r[4]),
                "attempted_at": r[5],
            }

    lessons = None
    current_course = None
    current_lesson = None
    lesson_list = None
    all_lessons = None
    quiz = None
    quiz_questions = None
    quiz_course_id = None
    next_lesson_id = None
    video_embed_url = None
    final_questions = None
    final_course = None
    final_result = None

    if view == "lesson" and rid:
        cursor.execute("SELECT * FROM courses WHERE id=?", (rid,))
        current_course = cursor.fetchone()
        cursor.execute("SELECT * FROM lessons WHERE course_id=? ORDER BY id", (rid,))
        lessons = cursor.fetchall()
    elif view == "lesson_content" and rid:
        cursor.execute("SELECT * FROM lessons WHERE id=?", (rid,))
        current_lesson = cursor.fetchone()
        if current_lesson:
            cursor.execute("SELECT * FROM courses WHERE id=?", (current_lesson[1],))
            current_course = cursor.fetchone()
            cursor.execute("SELECT * FROM lessons WHERE course_id=? ORDER BY id", (current_lesson[1],))
            lesson_list = cursor.fetchall()
            video_embed_url = get_video_embed_url(current_lesson[4] if len(current_lesson) > 4 else None)
    elif view == "lessons":
        q = (request.args.get("q") or "").strip().lower()
        course_filter = (request.args.get("course") or "").strip()
        if course_filter:
            cursor.execute("""
                SELECT lessons.*, courses.course
                FROM lessons JOIN courses ON courses.id = lessons.course_id
                WHERE lessons.course_id=?
                ORDER BY lessons.course_id, lessons.id
            """, (course_filter,))
        else:
            cursor.execute("""
                SELECT lessons.*, courses.course
                FROM lessons JOIN courses ON courses.id = lessons.course_id
                ORDER BY lessons.course_id, lessons.id
            """)
        rows = cursor.fetchall()
        if q:
            filtered = []
            for r in rows:
                if q in (r[2] or "").lower() or q in (r[3] or "").lower() or q in (r[5] or "").lower():
                    filtered.append(r)
            all_lessons = filtered
        else:
            all_lessons = rows
    elif view == "quiz" and rid:
        # Per-lesson quiz: multiple questions
        cursor.execute("SELECT * FROM lessons WHERE id=?", (rid,))
        lesson_row = cursor.fetchone()
        if lesson_row:
            cursor.execute("SELECT * FROM quiz_questions WHERE lesson_id=? ORDER BY id ASC", (rid,))
            quiz_questions = cursor.fetchall()
            cursor.execute("SELECT course_id FROM lessons WHERE id=?", (rid,))
            row = cursor.fetchone()
            quiz_course_id = row[0] if row else None
            quiz = {"lesson_id": rid, "lesson_title": lesson_row[2]}
            if quiz_course_id:
                cursor.execute(
                    "SELECT id FROM lessons WHERE course_id=? AND id>? ORDER BY id ASC LIMIT 1",
                    (quiz_course_id, rid),
                )
                nxt = cursor.fetchone()
                next_lesson_id = nxt[0] if nxt else None
    elif view == "final_quiz" and rid:
        # Final quiz = all lesson quizzes in this course (1 question per lesson)
        cursor.execute("SELECT * FROM courses WHERE id=?", (rid,))
        final_course = cursor.fetchone()
        cursor.execute("""
            SELECT qq.id, l.id as lesson_id, l.title, qq.question, qq.option1, qq.option2, qq.option3, qq.option4, qq.answer
            FROM lessons l
            JOIN quiz_questions qq ON qq.lesson_id = l.id
            WHERE l.course_id=?
            ORDER BY l.id ASC, qq.id ASC
        """, (rid,))
        final_questions = cursor.fetchall()
        final_result = request.args.get("result")

    # certificates: only show eligible courses
    eligible_cert_course_ids = []
    for c in my_courses:
        pid = str(c[0])
        p = course_progress.get(pid, {"total": 0, "completed": 0})
        a = assessments.get(pid)
        if p["total"] > 0 and p["completed"] >= p["total"] and a and a.get("passed"):
            eligible_cert_course_ids.append(pid)

    return render_template(
        "courses.html",
        view=view,
        courses=courses,
        my_courses=my_courses,
        lessons=lessons,
        current_course=current_course,
        current_lesson=current_lesson,
        lesson_list=lesson_list,
        all_lessons=all_lessons,
        quiz=quiz,
        quiz_questions=quiz_questions,
        quiz_course_id=quiz_course_id,
        lesson_counts=lesson_counts,
        completed_lessons=completed_lessons,
        completed_quizzes=completed_quizzes,
        course_progress=course_progress,
        assessments=assessments,
        pass_percent=PASS_PERCENT,
        video_embed_url=video_embed_url,
        next_lesson_id=next_lesson_id,
        eligible_cert_course_ids=eligible_cert_course_ids,
        final_course=final_course,
        final_questions=final_questions,
        final_result=final_result,
    )


# ENROLL
@app.route("/enroll/<course_id>", methods=["POST"])
def enroll(course_id):
    if not require_login():
        return redirect("/login")
    cursor.execute(
        "INSERT OR IGNORE INTO enrollments(user_id,course_id,enrolled_at) VALUES(?,?,?)",
        (session["user_id"], course_id, now_iso()),
    )
    conn.commit()
    return redirect("/courses?view=mycourses")


# MARK LESSON COMPLETE (auto when student clicks at end)
@app.route("/complete_lesson/<lesson_id>", methods=["POST"])
def complete_lesson(lesson_id):
    if not require_login():
        return redirect("/login")
    cursor.execute("SELECT course_id FROM lessons WHERE id=?", (lesson_id,))
    row = cursor.fetchone()
    if not row:
        abort(404)
    cursor.execute(
        "INSERT OR IGNORE INTO enrollments(user_id,course_id,enrolled_at) VALUES(?,?,?)",
        (session["user_id"], row[0], now_iso()),
    )
    cursor.execute(
        "INSERT OR IGNORE INTO lesson_progress(user_id,lesson_id,completed_at) VALUES(?,?,?)",
        (session["user_id"], lesson_id, now_iso()),
    )
    conn.commit()
    return redirect("/courses?view=lesson_content&id=" + str(lesson_id))


# SUBMIT QUIZ
@app.route("/submit_quiz", methods=["POST"])
def submit_quiz():
    if not require_login():
        return redirect("/login")
    lesson_id = request.form.get("lesson_id") or "1"
    cursor.execute("SELECT id, answer FROM quiz_questions WHERE lesson_id=? ORDER BY id ASC", (lesson_id,))
    rows = cursor.fetchall()
    total = len(rows)
    score = 0
    for qid, correct in rows:
        chosen = request.form.get(f"q_{qid}")
        if chosen is not None and correct is not None and normalize_quiz_value(chosen) == normalize_quiz_value(correct):
            score += 1
    percentage = int((score * 100) / total) if total else 0
    msg = f"Score: {score}/{total} ({percentage}%)"

    # store/overwrite latest quiz attempt (quiz completion tracking)
    cursor.execute(
        "INSERT OR REPLACE INTO quiz_attempts(user_id,lesson_id,score,total,percentage,attempted_at) VALUES(?,?,?,?,?,?)",
        (session["user_id"], lesson_id, score, total, percentage, now_iso()),
    )
    conn.commit()

    return redirect("/courses?view=quiz&id=" + str(lesson_id) + "&result=" + quote(msg))


# SUBMIT FINAL QUIZ (course-level)
@app.route("/submit_final_quiz/<course_id>", methods=["POST"])
def submit_final_quiz(course_id):
    if not require_login():
        return redirect("/login")

    # Make sure course exists
    cursor.execute("SELECT * FROM courses WHERE id=?", (course_id,))
    course = cursor.fetchone()
    if not course:
        abort(404)

    # Get final questions for course
    # Use the same multi-question table that the UI renders (`quiz_questions`)
    cursor.execute("""
        SELECT qq.id, qq.answer
        FROM lessons l
        JOIN quiz_questions qq ON qq.lesson_id = l.id
        WHERE l.course_id=?
        ORDER BY l.id ASC, qq.id ASC
    """, (course_id,))
    rows = cursor.fetchall()

    total = len(rows)
    score = 0
    for qid, correct in rows:
        chosen = request.form.get(f"q_{qid}")
        if chosen is not None and correct is not None and normalize_quiz_value(chosen) == normalize_quiz_value(correct):
            score += 1

    percentage = int((score * 100) / total) if total else 0
    passed = 1 if percentage >= PASS_PERCENT else 0

    cursor.execute(
        "INSERT INTO course_assessments(user_id,course_id,score,total,percentage,passed,attempted_at) VALUES(?,?,?,?,?,?,?)",
        (session["user_id"], course_id, score, total, percentage, passed, now_iso()),
    )
    conn.commit()

    result_msg = f"Score: {score}/{total} ({percentage}%). {'PASSED' if passed else 'FAILED'}"
    return redirect("/courses?view=final_quiz&id=" + str(course_id) + "&result=" + quote(result_msg))


# CERTIFICATE (PDF per course; only after 100% completion)
@app.route("/certificate/<course_id>")
def certificate(course_id):
    if not require_login():
        return redirect("/login")

    cursor.execute("SELECT * FROM courses WHERE id=?", (course_id,))
    course = cursor.fetchone()
    if not course:
        abort(404)

    cursor.execute("SELECT COUNT(*) FROM lessons WHERE course_id=?", (course_id,))
    total = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM lesson_progress lp
        JOIN lessons l ON l.id = lp.lesson_id
        WHERE lp.user_id=? AND l.course_id=?
    """, (session["user_id"], course_id))
    completed = cursor.fetchone()[0]

    # Require lesson completion
    if total == 0 or completed < total:
        abort(403)

    # Require final assessment pass
    cursor.execute("""
        SELECT score, total, percentage, passed
        FROM course_assessments
        WHERE user_id=? AND course_id=?
        ORDER BY attempted_at DESC, id DESC
        LIMIT 1
    """, (session["user_id"], course_id))
    a = cursor.fetchone()
    if not a or not a[3]:
        abort(403)

    pdf_bytes = generate_simple_certificate_pdf_bytes(session.get("user_name", "Student"), course[1])
    filename = f"certificate_{course[1].replace(' ', '_')}.pdf"
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True, download_name=filename, mimetype="application/pdf")


# ADMIN PANEL (admin details later, per your note)
@app.route("/admin")
def admin():
    view = request.args.get("view")
    cursor.execute("SELECT * FROM courses")
    courses = cursor.fetchall()
    cursor.execute("SELECT * FROM users")
    users = cursor.fetchall()
    cursor.execute("SELECT * FROM lessons")
    lessons = cursor.fetchall()

    # Admin dashboard metrics
    cursor.execute("SELECT COUNT(*) FROM enrollments")
    total_enrollments = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM lesson_progress")
    total_completions = cursor.fetchone()[0]

    cursor.execute("""
        SELECT e.enrolled_at, u.name, u.email, c.course, c.id
        FROM enrollments e
        JOIN users u ON u.id = e.user_id
        JOIN courses c ON c.id = e.course_id
        ORDER BY e.enrolled_at DESC
        LIMIT 10
    """)
    recent_enrollments = cursor.fetchall()

    cursor.execute("""
        SELECT lp.completed_at, u.name, c.course, l.title
        FROM lesson_progress lp
        JOIN users u ON u.id = lp.user_id
        JOIN lessons l ON l.id = lp.lesson_id
        JOIN courses c ON c.id = l.course_id
        ORDER BY lp.completed_at DESC
        LIMIT 10
    """)
    recent_completions = cursor.fetchall()

    cursor.execute("""
        SELECT c.id, c.course, COUNT(e.id) AS enrollments
        FROM courses c
        LEFT JOIN enrollments e ON e.course_id = c.id
        GROUP BY c.id
        ORDER BY enrollments DESC, c.course ASC
        LIMIT 10
    """)
    top_courses = cursor.fetchall()

    # Student progress: for each enrollment, get completed/total lessons + final quiz status
    cursor.execute("""
        SELECT
            u.id AS user_id, u.name, u.email,
            c.id AS course_id, c.course,
            COALESCE(lp.completed, 0) AS completed,
            COALESCE(lc.total, 0) AS total,
            ca.passed AS final_passed
        FROM enrollments e
        JOIN users u ON u.id = e.user_id
        JOIN courses c ON c.id = e.course_id
        LEFT JOIN (
            SELECT course_id, COUNT(*) AS total
            FROM lessons
            GROUP BY course_id
        ) lc ON lc.course_id = c.id
        LEFT JOIN (
            SELECT l.course_id, lp.user_id, COUNT(*) AS completed
            FROM lesson_progress lp
            JOIN lessons l ON l.id = lp.lesson_id
            GROUP BY l.course_id, lp.user_id
        ) lp ON lp.course_id = c.id AND lp.user_id = u.id
        LEFT JOIN course_assessments ca ON ca.user_id = u.id AND ca.course_id = c.id
            AND ca.id = (SELECT MAX(id) FROM course_assessments ca2 WHERE ca2.user_id = u.id AND ca2.course_id = c.id)
        ORDER BY u.name ASC, c.course ASC
    """)
    student_progress = cursor.fetchall()

    # Chart data: course-wise average progress %
    course_avg = {}
    for r in student_progress:
        cname = r[4]
        total_lessons = r[6] or 0
        completed = r[5] or 0
        pct = int((completed * 100) / total_lessons) if total_lessons else 0
        if cname not in course_avg:
            course_avg[cname] = []
        course_avg[cname].append(pct)
    course_progress_chart = [(c, sum(v) // len(v) if v else 0) for c, v in course_avg.items()]

    return render_template(
        "admin.html",
        courses=courses,
        users=users,
        lessons=lessons,
        view=view,
        total_enrollments=total_enrollments,
        total_completions=total_completions,
        recent_enrollments=recent_enrollments,
        recent_completions=recent_completions,
        top_courses=top_courses,
        student_progress=student_progress,
        course_progress_chart=course_progress_chart,
    )


@app.route("/addcourse", methods=["POST"])
def addcourse():
    name = request.form["course"]
    desc = request.form["description"]
    cursor.execute("INSERT INTO courses(course,description) VALUES(?,?)", (name, desc))
    conn.commit()
    return redirect("/admin")


@app.route("/delete/<id>")
def delete(id):
    cursor.execute("DELETE FROM courses WHERE id=?", (id,))
    conn.commit()
    return redirect("/admin")


@app.route("/update/<id>", methods=["POST"])
def update(id):
    name = request.form["course"]
    desc = request.form["description"]
    cursor.execute("UPDATE courses SET course=?, description=? WHERE id=?", (name, desc, id))
    conn.commit()
    return redirect("/admin")


@app.route("/addlesson", methods=["POST"])
def addlesson():
    course_id = request.form["course_id"]
    title = request.form["title"]
    content = request.form["content"]
    video_url = (request.form.get("video_url") or "").strip()
    audio_url = (request.form.get("audio_url") or "").strip()
    cursor.execute(
        "INSERT INTO lessons(course_id,title,content,video_url,audio_url) VALUES(?,?,?,?,?)",
        (course_id, title, content, video_url, audio_url),
    )
    conn.commit()
    return redirect("/admin?view=lessons")


@app.route("/deletelesson/<id>")
def deletelesson(id):
    cursor.execute("DELETE FROM lessons WHERE id=?", (id,))
    conn.commit()
    return redirect("/admin?view=lessons")


@app.route("/addquiz", methods=["POST"])
def addquiz():
    lesson_id = request.form["lesson_id"]
    question = request.form["question"]
    o1 = request.form["option1"]
    o2 = request.form["option2"]
    o3 = request.form["option3"]
    o4 = request.form["option4"]
    ans = request.form["answer"]
    cursor.execute(
        "INSERT INTO quiz(lesson_id,question,option1,option2,option3,option4,answer) VALUES(?,?,?,?,?,?,?)",
        (lesson_id, question, o1, o2, o3, o4, ans),
    )
    conn.commit()
    return redirect("/admin")


# LOGOUT
@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    session.pop("user_email", None)
    return redirect("/")


app.run(debug=True)