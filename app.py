import os
import re
import random
import json
from recommendation_engine import get_recommended_projects
import secrets
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from pymongo import MongoClient
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from bson.objectid import ObjectId
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from dotenv import load_dotenv
from markupsafe import Markup
from ai_roadmap_generator import configure_ai, generate_roadmap_with_ai, find_youtube_playlist
import regex as re_ext
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

# --- Load environment variables and configure AI ---
load_dotenv()
try:
    if not os.getenv('GEMINI_API_KEY'):
        print("WARNING: GEMINI_API_KEY not found in .env file. AI features will likely fail.")
    configure_ai()
    print("AI configured successfully.")
except ValueError as e:
    print(f"AI Configuration Error: {e}")
except Exception as e_ai_config:
     print(f"An unexpected error occurred during AI configuration: {e_ai_config}")


# --- Flask App Initialization ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'project_uploads')
bcrypt = Bcrypt(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))
s = URLSafeTimedSerializer(app.secret_key)

# --- Email Configuration ---
if not os.getenv('MAIL_USERNAME') or not os.getenv('MAIL_PASSWORD'):
    print("WARNING: Email credentials not found in .env file. Email features will likely fail.")
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')
mail = Mail(app)

# --- Database Connection ---
try:
    mongo_uri = "mongodb://127.0.0.1:27017/skillbridge_db"
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    db = client.get_database()
    client.admin.command('ismaster')
    print(f"MongoDB connection successful.")

    users_collection = db['users']
    roadmaps_collection = db['roadmaps']
    projects_collection = db['projects']
    commits_collection = db['commits']
    messages_collection = db['messages']
    communities_collection = db["communities"]
    community_messages_collection = db["community_messages"]

 
except Exception as e:
    print(f"ERROR: Could not connect to MongoDB. Please ensure it's running. Details: {e}")
    exit()


# --- Flask-Login Configuration ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"

class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data["_id"])
        self.email = user_data.get("email", "N/A")
        self.name = user_data.get("name", "User")

@login_manager.user_loader
def load_user(user_id):
    try:
        obj_id = ObjectId(user_id)
        user_data = users_collection.find_one({"_id": obj_id})
        if user_data:
            return User(user_data)
    except Exception as e:
        print(f"Error loading user {user_id}: {e}")
    return None

# --- Helper Function for Portfolio ---
def flatten_data(y):
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x:
                flatten(x.get(a), name + str(a) + '_')
        elif isinstance(x, list):
            pass
        elif x is not None:
            out[name[:-1]] = x
    flatten(y)
    return out

# --- Main Routes (Auth, Profile, etc.) ---

@app.route('/')
def index():
    try:
        # Total users joined
        total_users = users_collection.count_documents({})

        # Total projects uploaded
        total_projects = projects_collection.count_documents({})

        # Completed projects = projects having at least one commit
        completed_project_ids = commits_collection.distinct("project_id")
        completed_projects = len(completed_project_ids)

        # Completion percentage
        completion_percent = 0
        if total_projects > 0:
            completion_percent = int((completed_projects / total_projects) * 100)

        return render_template(
            'index.html',
            total_users=total_users,
            total_projects=total_projects,
            completed_projects=completed_projects,
            completion_percent=completion_percent
        )

    except Exception as e:
        print(f"Index stats error: {e}")
        return render_template(
            'index.html',
            total_users=0,
            total_projects=0,
            completed_projects=0,
            completion_percent=0
        )


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not name or not email or not password:
             flash("All fields are required.", "error"); return redirect(url_for('signup'))
        if password != confirm_password:
            flash("Passwords do not match.", "error"); return redirect(url_for('signup'))

        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash("Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol (@$!%*?&).", "error"); return redirect(url_for('signup'))

        if users_collection.find_one({'email': email}):
            flash("An account with this email already exists. Try logging in.", "error"); return redirect(url_for('login'))

        otp = random.randint(100000, 999999)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        session['temp_user_data'] = {'name': name, 'email': email, 'password': hashed_password}
        session['otp'] = otp
        session['otp_timestamp'] = datetime.utcnow().timestamp()

        try:
            msg = Message('Your SkillBridge OTP Code', recipients=[email])
            msg.body = f'Your One-Time Password (OTP) for SkillBridge is: {otp}. It expires in 10 minutes.'
            mail.send(msg)
            flash('An OTP has been sent to your email. Please check your inbox (and spam folder).', 'success');
            return redirect(url_for('verify'))
        except Exception as e:
            print(f"Failed to send OTP email to {email}: {e}")
            flash(f'Failed to send verification email. Please try again later or contact support.', 'error');
            session.pop('temp_user_data', None)
            session.pop('otp', None)
            session.pop('otp_timestamp', None)
            return redirect(url_for('signup'))

    return render_template('signup.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'temp_user_data' not in session or 'otp' not in session or 'otp_timestamp' not in session:
        flash('Verification session expired or invalid. Please sign up again.', 'warning')
        return redirect(url_for('signup'))

    otp_age = datetime.utcnow().timestamp() - session.get('otp_timestamp', 0)
    if otp_age > 600: 
        session.pop('temp_user_data', None)
        session.pop('otp', None)
        session.pop('otp_timestamp', None)
        flash('Your OTP has expired. Please sign up again.', 'error')
        return redirect(url_for('signup'))


    if request.method == 'POST':
        user_otp_str = request.form.get('otp')
        try:
            user_otp = int(user_otp_str)
            stored_otp = session.get('otp')
            if stored_otp is not None and user_otp == stored_otp:
                user_data = session.pop('temp_user_data', None)
                session.pop('otp', None)
                session.pop('otp_timestamp', None)
                if user_data:
                    user_data['profile_pic'] = 'default.jpg'
                    user_data['github_url'] = ''
                    user_data['linkedin_url'] = ''
                    user_data['known_skills'] = []
                    user_data['learning_skills'] = []
                    user_data['created_at'] = datetime.utcnow()

                    users_collection.insert_one(user_data)
                    flash('Email verified successfully! Please log in.', 'success'); return redirect(url_for('login'))
                else:
                    flash('Session error retrieving user data. Please sign up again.', 'error'); return redirect(url_for('signup'))
            else:
                flash('Invalid OTP. Please try again.', 'error'); return redirect(url_for('verify'))
        except (ValueError, TypeError):
             flash('Invalid OTP format. Please enter numbers only.', 'error'); return redirect(url_for('verify'))

    remaining_time = max(0, 600 - int(otp_age))
    return render_template('verify.html', remaining_minutes=remaining_time // 60, remaining_seconds=remaining_time % 60)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        remember = bool(request.form.get('remember'))

        if not email or not password:
             flash("Email and password are required.", "error"); return redirect(url_for('login'))

        user_data = users_collection.find_one({'email': email})

        if user_data and bcrypt.check_password_hash(user_data.get('password', ''), password):
            user = User(user_data)
            login_user(user, remember=remember);
            next_page = request.args.get('next')
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(next_page or url_for('main_page'))
        else:
            flash("Invalid email or password. Please try again.", "error"); return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if email and users_collection.find_one({'email': email}, {'_id': 1}):
            token = s.dumps(email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            try:
                msg = Message('Password Reset Request for SkillBridge', recipients=[email])
                msg.body = f'Click the following link to reset your password: {reset_url}\n\nThis link will expire in 1 hour.'
                mail.send(msg)
            except Exception as e:
                print(f"Failed to send password reset email to {email}: {e}")
        
        flash('If an account exists for that email, a password reset link has been sent.', 'info');
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=3600)
    except (SignatureExpired, BadTimeSignature):
        flash('The password reset link is invalid or has expired.', 'error'); return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if password != confirm_password:
            flash('Passwords do not match.', 'error'); return render_template('reset_password.html', token=token)
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash('Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol.', 'error'); return render_template('reset_password.html', token=token)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        result = users_collection.update_one({'email': email}, {'$set': {'password': hashed_password}})
        if result.modified_count > 0:
            flash('Your password has been updated successfully! Please log in with your new password.', 'success'); return redirect(url_for('login'))
        else:
            flash('Could not update password. Please try again.', 'error'); return redirect(url_for('forgot_password'))
    return render_template('reset_password.html', token=token)


@app.route('/mainpage')
@login_required
def main_page():
    user_name = current_user.name if current_user.is_authenticated else "User"
    
    # Fetch actual counts from DB
    roadmap_count = roadmaps_collection.count_documents({'user_id': ObjectId(current_user.id)})
    project_count = projects_collection.count_documents({'created_by_id': ObjectId(current_user.id)})
    
    return render_template(
        'mainpage.html', 
        user_name=user_name,
        roadmap_count=roadmap_count,
        project_count=project_count
    )

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('index'))

# --- UPDATED: PROFILE ROUTE WITH ALL NEW FIELDS ---
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        old_user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        old_learning_skills = set(old_user_data.get('learning_skills', []))

        profile_pic_fn = old_user_data.get('profile_pic', 'default.jpg')
        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                random_hex = secrets.token_hex(8)
                _, f_ext = os.path.splitext(filename)
                profile_pic_fn = random_hex + f_ext
                file.save(os.path.join(app.root_path, 'static/profile_pics', profile_pic_fn))

        new_learning_skills_list = [s.strip() for s in request.form.get('learning_skills', '').split(',') if s.strip()]
        
        # SAVING ALL INPUTS INCLUDING NEW PROFESSIONAL/SOCIAL FIELDS
        update_data = {
            'name': request.form.get('name', '').strip(),
            'title': request.form.get('title', '').strip(),
            'about_me': request.form.get('about_me', '').strip(),
            'location': request.form.get('location', '').strip(),
            'github_url': request.form.get('github_url', '').strip(),
            'linkedin_url': request.form.get('linkedin_url', '').strip(),
            'instagram_url': request.form.get('instagram_url', '').strip(),
            'facebook_url': request.form.get('facebook_url', '').strip(),
            'education_college': request.form.get('education_college', '').strip(),
            'education_degree': request.form.get('education_degree', '').strip(),
            'experience_years': request.form.get('experience_years', '').strip(),
            'current_status': request.form.get('current_status', '').strip(),
            'availability': request.form.get('availability', '').strip(),
            'career_goal': request.form.get('career_goal', '').strip(),
            'known_skills': [s.strip() for s in request.form.get('known_skills', '').split(',') if s.strip()],
            'learning_skills': new_learning_skills_list,
            'profile_pic': profile_pic_fn
        }

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        flash('Profile updated successfully!', 'success')

        new_learning_skills = set(new_learning_skills_list)
        added_skills = list(new_learning_skills - old_learning_skills)
        if added_skills:
            goal_str = added_skills[0]
            link = url_for('roadmap_generator', goal=goal_str)
            message = Markup(f'New goal detected! 🚀 <a href="{link}">Generate a roadmap for "{goal_str}"?</a>')
            flash(message, 'info')

        return redirect(url_for('profile'))

    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
    profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)
    
    # MAPPING ALL FIELDS FOR THE TEMPLATE
    user_profile = {
        'name': user_data.get('name', ''),
        'email': user_data.get('email', ''),
        'title': user_data.get('title', ''),
        'about_me': user_data.get('about_me', ''),
        'location': user_data.get('location', ''),
        'github_url': user_data.get('github_url', ''),
        'linkedin_url': user_data.get('linkedin_url', ''),
        'instagram_url': user_data.get('instagram_url', ''),
        'facebook_url': user_data.get('facebook_url', ''),
        'education_college': user_data.get('education_college', ''),
        'education_degree': user_data.get('education_degree', ''),
        'experience_years': user_data.get('experience_years', ''),
        'current_status': user_data.get('current_status', ''),
        'availability': user_data.get('availability', ''),
        'career_goal': user_data.get('career_goal', ''),
        'known_skills_str': ', '.join(user_data.get('known_skills', [])),
        'learning_skills_str': ', '.join(user_data.get('learning_skills', [])),
        'profile_pic_url': profile_pic_url
    }
    return render_template('profile.html', user=user_profile)


# --- Roadmap Routes ---

@app.route('/roadmap_generator', methods=['GET', 'POST'])
@login_required
def roadmap_generator():
    roadmap_data = None
    goal = "" 

    if request.method == 'POST':
        goal = request.form.get('goal', '').strip()
        if not goal:
            flash("Please enter a goal for your roadmap.", "error")
            return render_template('roadmap_generator.html', goal=goal)
    
    else: 
        goal_from_url = request.args.get('goal', '').strip()
        if goal_from_url:
            goal = goal_from_url 
            flash(f"Generating roadmap for '{goal}'...", 'info')

    if goal:
        try:
            print(f"Calling Gemini AI with FINAL prompt for '{goal}'...")
            roadmap_data = generate_roadmap_with_ai(goal)

            if roadmap_data and isinstance(roadmap_data, dict) and isinstance(roadmap_data.get('stages'), list):
                for stage in roadmap_data.get("stages", []):
                    learning_modules = stage.get("learning_modules", [])
                    if not isinstance(learning_modules, list): continue
                    for module in learning_modules:
                         resources = module.get("resources", [])
                         if not isinstance(resources, list): continue
                         for resource in resources:
                            if isinstance(resource, dict) and resource.get("type") == "Free YouTube Playlist":
                                query = resource.get("youtube_search_query", goal)
                                try:
                                    url, title = find_youtube_playlist(query)
                                    resource["url"] = url if url else "#"
                                    resource["title"] = title if title else f"Playlist for: {query}"
                                except Exception as e_yt:
                                    print(f"Error finding YouTube playlist for '{query}': {e_yt}")
                                    resource["url"] = "#"
                                    resource["title"] = f"Error finding playlist"
                
                return render_template('roadmap_generator.html', roadmap_data=roadmap_data, goal=goal)
            else:
                print(f"AI response invalid or missing stages for goal '{goal}'. Response: {roadmap_data}")
                flash("Sorry, the AI response was incomplete or in an unexpected format. Please try again.", "error")
        
        except Exception as e_ai:
            print(f"Error during roadmap generation or processing for '{goal}': {e_ai}")
            flash(f"An error occurred while communicating with the AI: {e_ai}", "error")
        
        return render_template('roadmap_generator.html', goal=goal)

    return render_template('roadmap_generator.html')

@app.route('/save_roadmap', methods=['POST'])
@login_required
def save_roadmap():
    goal = request.form.get('goal')
    roadmap_content_str = request.form.get('roadmap_content')
    if goal and roadmap_content_str:
        try:
            roadmap_data = json.loads(roadmap_content_str)
            if isinstance(roadmap_data, dict) and 'stages' in roadmap_data:
                roadmaps_collection.insert_one({
                    'user_id': ObjectId(current_user.id),
                    'goal': goal,
                    'roadmap_content': roadmap_data,
                    'created_at': datetime.utcnow()
                 })
                flash('Roadmap saved successfully!', 'success')
                return redirect(url_for('my_roadmaps'))
            else:
                flash('Invalid roadmap data format received from the form.', 'error')
        except json.JSONDecodeError:
             print("Error decoding roadmap JSON from form.")
             flash('Internal error processing roadmap data. Could not save.', 'error')
        except Exception as e:
             print(f"Error saving roadmap to DB: {e}")
             flash('An unexpected error occurred while saving the roadmap.', 'error')
    else:
        flash('Could not save roadmap. Goal or content was missing from the request.', 'error')
    return redirect(url_for('roadmap_generator'))


@app.route('/my_roadmaps')
@login_required
def my_roadmaps():
    try:
        user_roadmaps = list(roadmaps_collection.find({'user_id': ObjectId(current_user.id)}).sort('created_at', -1))
        return render_template('my_roadmaps.html', roadmaps=user_roadmaps)
    except Exception as e:
        print(f"Error fetching roadmaps for user {current_user.id}: {e}")
        flash("Could not load your saved roadmaps.", "error")
        return render_template('my_roadmaps.html', roadmaps=[])


@app.route('/roadmap/<roadmap_id>')
@login_required
def view_roadmap(roadmap_id):
    try:
        obj_id = ObjectId(roadmap_id)
    except Exception:
        flash("Invalid roadmap ID format.", "error")
        return redirect(url_for('my_roadmaps'))

    roadmap = roadmaps_collection.find_one({'_id': obj_id})
    
    if not roadmap or str(roadmap.get('user_id')) != current_user.id:
        flash("Roadmap not found or permission denied.", "error")
        return redirect(url_for('my_roadmaps'))

    # --- FORCE DICTIONARY CONVERSION ---
    content = roadmap.get('roadmap_content')
    
    # Logic: If content is text (string), convert it back into a Python object (dict)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            print("Failed to parse roadmap_content string")
            content = {}

    # Handle cases where data might be wrapped twice
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except:
            pass

    # Re-assign the clean dictionary back to the roadmap object
    roadmap['roadmap_content'] = content if isinstance(content, dict) else {}
    # -----------------------------------

    progress_percentage = 0
    try:
        # Access the stages safely from the now-guaranteed dictionary
        stages = roadmap['roadmap_content'].get('stages', [])
        if isinstance(stages, list) and len(stages) > 0:
            completed_count = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
            progress_percentage = (completed_count / len(stages)) * 100
    except Exception as e:
        print(f"Progress calculation error: {e}")

    return render_template('view_roadmap.html', roadmap=roadmap, progress_percentage=progress_percentage)

@app.route('/complete_stage/<roadmap_id>/<int:stage_index>')
@login_required
def complete_stage(roadmap_id, stage_index):
    try:
        obj_id = ObjectId(roadmap_id)
        roadmap = roadmaps_collection.find_one({'_id': obj_id, 'user_id': ObjectId(current_user.id)})
        
        if not roadmap:
            return redirect(url_for('my_roadmaps'))

        # ✅ Ensure we are working with a dictionary
        content = roadmap.get('roadmap_content')
        if isinstance(content, str):
            content = json.loads(content)

        # Update the stage in the dictionary
        if 0 <= stage_index < len(content['stages']):
            content['stages'][stage_index]['completed'] = True
            
            # Save the entire updated object back to DB
            roadmaps_collection.update_one(
                {'_id': obj_id},
                {'$set': {'roadmap_content': content}}
            )
            flash('Stage marked as complete!', 'success')
            
        return redirect(url_for('view_roadmap', roadmap_id=roadmap_id))
    except Exception as e:
        print(f"Update error: {e}")
        return redirect(url_for('my_roadmaps'))

# --- PROJECT SYSTEM ROUTES ---

@app.route('/projects')
def projects():
    try:
        all_projects_cursor = projects_collection.find().sort('created_at', -1)
        all_projects = list(all_projects_cursor)

        recommended_projects = []
        other_projects = []
        profile_incomplete = False
        is_authenticated = current_user.is_authenticated

        if is_authenticated:
            # Using the recommendation engine logic
            recommended_projects = get_recommended_projects(current_user.id, users_collection, projects_collection)
            
            rec_ids = [p['_id'] for p in recommended_projects]
            other_projects = list(projects_collection.find({'_id': {'$nin': rec_ids}}).sort('created_at', -1))

            user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'known_skills': 1, 'learning_skills': 1})
            if not user_data.get('known_skills') and not user_data.get('learning_skills'):
                profile_incomplete = True
        else:
            other_projects = all_projects

        return render_template('projects.html',
                               recommended_projects=recommended_projects,
                               other_projects=other_projects,
                               profile_incomplete=profile_incomplete,
                               is_authenticated=is_authenticated)

    except Exception as e:
        print(f"Error fetching community projects: {e}")
        flash("Could not load community projects at this time.", "error")
        return render_template('projects.html', recommended_projects=[], other_projects=[], profile_incomplete=False, is_authenticated=current_user.is_authenticated)


@app.route('/my_projects')
@login_required
def my_projects():
    try:
        my_projects_list = list(projects_collection.find({
            'created_by_id': ObjectId(current_user.id)
        }).sort('created_at', -1))
        return render_template('my_projects.html', projects=my_projects_list)
    except Exception as e:
         print(f"Error fetching user projects for {current_user.id}: {e}")
         flash("Could not load your projects.", "error")
         return render_template('my_projects.html', projects=[])


@app.route('/create_project', methods=['GET', 'POST'])
@login_required
def create_project():

    if request.method == 'POST':

        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        skills_str = request.form.get('skills', '')
        skills = [s.strip() for s in skills_str.split(',') if s.strip()]

        if not title or not description:
            flash("Title and Description are required.", "error")
            return render_template("create_project.html")

        # Create project first
        project_id = projects_collection.insert_one({
            "title": title,
            "description": description,
            "skills_needed": skills,
            "created_by_id": ObjectId(current_user.id),
            "created_by_name": current_user.name,
            "created_at": datetime.utcnow()
        }).inserted_id

        create_community = request.form.get('create_community')

        if create_community == "yes":

            primary = request.form.get('community_skill_primary', '').strip()

            if not primary:
                flash("Primary skill required for community.", "error")
                return render_template("create_project.html")

            secondary = request.form.get('community_skill_secondary', '').strip()
            tool = request.form.get('community_skill_tool', '').strip()
            domain = request.form.get('community_skill_domain', '').strip()
            optional = request.form.get('community_skill_optional', '').strip()

            visibility = request.form.get('community_visibility', 'public')

            skills_required = [
                s for s in [primary, secondary, tool, domain, optional] if s
            ]

            community_id = communities_collection.insert_one({
                "project_id": project_id,
                "project_title": title,
                "skills_required": skills_required,
                "visibility": visibility,
                "owner_id": ObjectId(current_user.id),
                "owner_name": current_user.name,
                "members": [ObjectId(current_user.id)],
                "admins": [],
                "pending_requests": [],
                "created_at": datetime.utcnow()
            }).inserted_id

            # Link project to community
            projects_collection.update_one(
                {"_id": project_id},
                {"$set": {"community_id": community_id}}
            )

            flash("Project and community created successfully!", "success")
            return redirect(url_for("find_communities"))

        flash("Project created successfully!", "success")
        return redirect(url_for("my_projects"))

    return render_template("create_project.html")



@app.route('/project/<project_id>')
def view_project(project_id):
    try:
        obj_id = ObjectId(project_id)
    except Exception:
        flash("Invalid project ID format.", "error")
        return redirect(url_for('projects'))

    project = projects_collection.find_one({'_id': obj_id})
    if not project:
        flash('Project not found.', 'error')
        return redirect(url_for('projects'))

    is_owner = False
    if current_user.is_authenticated and str(project.get('created_by_id')) == current_user.id:
         is_owner = True

    try:
        commits = list(commits_collection.find({'project_id': obj_id}).sort('timestamp', -1))
    except Exception as e:
         print(f"Error fetching commits for project {project_id}: {e}")
         flash("Could not load project history.", "error")
         commits = []

    creator_name = project.get('created_by_name', 'Unknown User')

    return render_template('project_page.html', project=project, is_owner=is_owner, commits=commits, creator_name=creator_name)


@app.route('/project/<project_id>/upload', methods=['GET', 'POST'])
@login_required
def upload_version(project_id):
    try:
        obj_id = ObjectId(project_id)
        project = projects_collection.find_one({'_id': obj_id})
    except Exception:
        flash('Invalid project ID format.', 'error')
        return redirect(url_for('my_projects'))

    if not project:
        flash('Project not found.', 'error')
        return redirect(url_for('my_projects'))

    if str(project.get('created_by_id')) != current_user.id:
        flash('You are not authorized to upload to this project.', 'error')
        return redirect(url_for('view_project', project_id=project_id))

    if request.method == 'POST':
        commit_message = request.form.get('message', '').strip()
        if not commit_message:
            flash('A version message is required.', 'error')
            return render_template('upload_version.html', project=project)

        if 'project_file' not in request.files or not request.files['project_file'].filename:
            flash('You must select a .zip file to upload.', 'error')
            return render_template('upload_version.html', project=project)

        file = request.files['project_file']
        original_filename = secure_filename(file.filename)
        _, f_ext = os.path.splitext(original_filename)

        if f_ext.lower() != '.zip':
             flash('Only .zip files are allowed.', 'error')
             return render_template('upload_version.html', project=project)

        random_hex = secrets.token_hex(8)
        project_filename = random_hex + f_ext
        upload_dir = app.config['UPLOAD_FOLDER']
        file_path = os.path.join(upload_dir, project_filename)

        try:
            os.makedirs(upload_dir, exist_ok=True)
            file.save(file_path)

            commits_collection.insert_one({
                'project_id': obj_id,
                'user_id': ObjectId(current_user.id),
                'user_name': current_user.name,
                'timestamp': datetime.utcnow(),
                'message': commit_message,
                'filename': project_filename
            })

            flash('New project version uploaded successfully!', 'success')
            return redirect(url_for('view_project', project_id=project_id))

        except Exception as e:
            print(f"Error saving file or commit for project {project_id}: {e}")
            flash('An error occurred during upload. Please try again.', 'error')
            if os.path.exists(file_path):
                 try:
                     os.remove(file_path)
                     print(f"Removed partially uploaded file: {file_path}")
                 except OSError as e_rm:
                     print(f"Error removing partially uploaded file {file_path}: {e_rm}")
            return render_template('upload_version.html', project=project)

    return render_template('upload_version.html', project=project)

@app.route('/download_project/<filename>')
def download_project(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
         flash('Invalid filename.', 'error')
         return redirect(url_for('projects'))

    try:
        directory = app.config['UPLOAD_FOLDER']
        if not os.path.abspath(directory).startswith(os.path.abspath(os.path.join(app.root_path, 'static'))):
             print(f"Attempted directory traversal: {directory}")
             flash('Access denied.', 'error')
             return redirect(url_for('projects'))

        return send_from_directory(directory, safe_filename, as_attachment=True)

    except FileNotFoundError:
        flash(f'The requested project file ({safe_filename}) was not found.', 'error')
        referrer = request.referrer
        if referrer and ('/project/' in referrer or '/projects' in referrer or '/my_projects' in referrer):
             return redirect(referrer)
        return redirect(url_for('projects'))
    except Exception as e:
        print(f"Error downloading file {filename}: {e}")
        flash('An error occurred while downloading the file.', 'error')
        return redirect(url_for('projects'))


# --- PORTFOLIO BUILDER ROUTES ---

@app.route('/portfolio_builder')
@login_required
def portfolio_builder():
    return render_template('portfolio_builder.html')

@app.route('/portfolio_assets/<path:filename>') 
def serve_portfolio_assets(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
         return "Invalid filename", 400
    try:
        directory = os.path.join(app.root_path, 'static', 'portfolio_assets')
        return send_from_directory(directory, safe_filename)
    except FileNotFoundError:
         print(f"Portfolio asset not found: {filename}")
         return "Asset not found", 404

@app.route('/portfolio_img/<path:filename>') 
def serve_portfolio_images(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return "Invalid filename", 400
    try:
        directory = os.path.join(app.root_path, 'static', 'portfolio_img')
        return send_from_directory(directory, safe_filename)
    except FileNotFoundError:
        print(f"Portfolio image not found: {filename}")
        return "Image not found", 404
    except Exception as e:
        print(f"Error serving portfolio image {filename}: {e}")
        return "Error serving image", 500


@app.route("/api/templates")
@login_required
def list_templates():
    templates = []
    template_html_dir = os.path.join(app.root_path, 'templates', 'portfolio_templates')
    static_img_dir = os.path.join(app.root_path, 'static', 'portfolio_img')

    if not os.path.isdir(template_html_dir):
        print(f"Error: Portfolio templates directory not found at {template_html_dir}")
        return jsonify({"error": "Portfolio templates directory not found"}), 500
    try:
        for filename in os.listdir(template_html_dir):
            if filename.endswith(".html"):
                template_id = filename.replace('.html', '')
                safe_template_id = secure_filename(template_id)
                thumb_filename = f"{safe_template_id}_thumb.png"
                thumb_path = os.path.join(static_img_dir, thumb_filename)

                if os.path.isfile(thumb_path):
                    templates.append({
                        "id": template_id,
                        "name": template_id.replace('_', ' ').title(),
                        "thumbnail": url_for('serve_portfolio_images', filename=thumb_filename)
                    })
                else:
                     print(f"Warning: Thumbnail not found for template {template_id} at {thumb_path}")

        return jsonify(templates)
    except Exception as e:
        print(f"Error listing portfolio templates: {e}")
        return jsonify({"error": "Failed to list templates"}), 500


@app.route("/api/template/<template_id>")
@login_required
def get_template_details(template_id):
    safe_template_id = secure_filename(template_id)
    try:
        filepath = os.path.join(app.root_path, 'templates', 'portfolio_templates', f"{safe_template_id}.html")
        if not os.path.isfile(filepath):
             return jsonify({"error": "Template not found"}), 404

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user_data:
             return jsonify({"error": "User data not found"}), 500

        user_projects = list(projects_collection.find({'created_by_id': ObjectId(current_user.id)}))

        live_portfolio_data = {
            "header_name": user_data.get('name', 'Your Name'),
            "header_title": user_data.get('title', 'Web Developer'),
            "header_location": user_data.get('location', 'Your Location'),
            "contact_email": user_data.get('email', ''),
            "contact_linkedin": user_data.get('linkedin_url', ''),
            "contact_github": user_data.get('github_url', ''),
            "about_description": user_data.get('about_me', "A passionate developer building the future."),
            "edu_college": user_data.get('education_college', 'Your University'),
            "edu_degree": user_data.get('education_degree', 'Your Degree'),
            "skills_list": ", ".join(user_data.get('known_skills', []))
        }

        formatted_projects = []
        for p in user_projects:
            formatted_projects.append({
                "title": p.get("title", "Untitled Project"),
                "description": p.get("description", "Project description goes here."),
                "skills_needed": p.get("skills_needed", [])
            })

        defaults_flat = flatten_data(live_portfolio_data)
        soup = BeautifulSoup(content, 'html.parser')
        form_fields = []

        for img_tag in soup.find_all('img'):
            src = img_tag.get('src')
            if src and not src.startswith(('http', '/', 'data:', '{{')):
                img_tag['src'] = url_for('serve_portfolio_assets', filename=src)

        for link_tag in soup.find_all('link', rel='stylesheet'):
            href = link_tag.get('href')
            if href and not href.startswith(('http', '/', 'data:')):
                href_filename = os.path.basename(href)
                link_tag['href'] = url_for('serve_portfolio_assets', filename=href_filename)
        
        for script_tag in soup.find_all('script', src=True):
            src = script_tag.get('src')
            if src and not src.startswith(('http', '/', 'data:')):
                script_tag['src'] = url_for('serve_portfolio_assets', filename=src)

        script_tag_pattern = re_ext.compile(r'const\s+portfolioData\s*=')
        script_to_remove = soup.find('script', string=script_tag_pattern)
        if script_to_remove:
            script_to_remove.decompose()

        for element in soup.find_all(attrs={'data-content': True}):
            field_id = element['data-content']
            target_attr = 'innerText'
            
            if element.name == 'a': target_attr = 'href'
            elif element.name == 'img': target_attr = 'src'
            
            if target_attr == 'innerText':
                element['contenteditable'] = 'true'
            
            val = live_portfolio_data.get(field_id, "")
            form_fields.append({"id": field_id, "targetAttribute": target_attr, "defaultValue": str(val)})
            element['data-binding'] = field_id
            del element['data-content']

        return jsonify({
            "config": {"fields": form_fields},
            "list_data": {"projects": formatted_projects},
            "htmlContent": str(soup)
        })

    except Exception as e:
        print(f"Error serving template: {e}")
        return jsonify({"error": str(e)}), 500


# --- VIEW PUBLIC PROFILE ROUTE ---
@app.route('/user/<user_id>')
def view_user_profile(user_id):
    try:
        obj_id = ObjectId(user_id)
        # Find user data in MongoDB
        user_data = users_collection.find_one({'_id': obj_id})
        
        if not user_data:
            flash("User not found.", "error")
            return redirect(url_for('projects'))

        # Prepare social links for the UI
        profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
        profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)

        user_profile = {
            'id': str(user_data['_id']),
            'name': user_data.get('name', 'Builder'),
            'title': user_data.get('title', 'Developer'),
            'about_me': user_data.get('about_me', ''),
            'experience_years': user_data.get('experience_years', '0'),
            'current_status': user_data.get('current_status', 'Available'),
            'location': user_data.get('location', 'Remote'),
            'education_college': user_data.get('education_college', 'N/A'),
            'education_degree': user_data.get('education_degree', 'N/A'),
            'career_goal': user_data.get('career_goal', ''),
            'github_url': user_data.get('github_url', ''),
            'linkedin_url': user_data.get('linkedin_url', ''),
            'instagram_url': user_data.get('instagram_url', ''),
            'facebook_url': user_data.get('facebook_url', ''),
            'portfolio_url': user_data.get('portfolio_url', ''),
            'profile_pic_url': profile_pic_url,
            'known_skills': user_data.get('known_skills', []),
            'learning_skills': user_data.get('learning_skills', [])
        }
        
        return render_template('view_profile.html', profile=user_profile)
    except Exception as e:
        print(f"Error viewing profile: {e}")
        return redirect(url_for('projects'))


# --- MESSAGING ROUTES ---

@app.route('/messages')
@login_required
def messages_list():
    try:
        u_id = ObjectId(current_user.id)

        pipeline = [
            {"$match": {"$or": [{"sender_id": u_id}, {"receiver_id": u_id}]}},
            {"$sort": {"timestamp": -1}},
            {"$group": {
                "_id": {
                    "$cond": [
                        {"$eq": ["$sender_id", u_id]},
                        "$receiver_id",
                        "$sender_id"
                    ]
                },
                "last_message": {"$first": "$content"},
                "timestamp": {"$first": "$timestamp"},
                "is_read": {"$first": "$is_read"},
                "last_sender": {"$first": "$sender_id"}
            }},
            {"$sort": {"timestamp": -1}}
        ]

        results = list(messages_collection.aggregate(pipeline))
        conversations = []

        for res in results:
            other_user = users_collection.find_one({"_id": res["_id"]})
            if not other_user:
                continue

            sent_by_me = str(res["last_sender"]) == current_user.id

            # 👇 determine sender name
            if sent_by_me:
                sender_name = "Me"
            else:
                sender_user = users_collection.find_one({"_id": res["last_sender"]})
                sender_name = sender_user.get("name", "User") if sender_user else "User"

            is_unread = (not res["is_read"]) and (not sent_by_me)

            conversations.append({
                "user_id": str(other_user["_id"]),
                "user_name": other_user.get("name", "Unknown"),
                "profile_pic": other_user.get("profile_pic", "default.jpg"),
                "last_message": res.get("last_message", ""),
                "timestamp": res["timestamp"].strftime("%b %d, %I:%M %p"),
                "is_unread": is_unread,
                "sent_by_me": sent_by_me,
                "sender_name": sender_name   # ✅ IMPORTANT
            })

        return render_template("messages_list.html", conversations=conversations)

    except Exception as e:
        print(f"Inbox error: {e}")
        return redirect(url_for("main_page"))


    
@app.route('/chat/<receiver_id>', methods=['GET', 'POST'])
@login_required
def chat(receiver_id):
    try:
        r_id = ObjectId(receiver_id)
        u_id = ObjectId(current_user.id)
        rec = users_collection.find_one({"_id": r_id})
        
        if not rec:
            flash("User not found.", "error")
            return redirect(url_for('messages_list'))

        if request.method == 'POST':
            msg = request.form.get('content', '').strip()
            if msg:
                messages_collection.insert_one({
                    "sender_id": u_id,
                    "receiver_id": r_id,
                    "content": msg,
                    "timestamp": datetime.utcnow(),
                    "is_read": False
                })
            return redirect(url_for('chat', receiver_id=receiver_id))

        # Mark messages I received as read
        messages_collection.update_many(
            {"sender_id": r_id, "receiver_id": u_id, "is_read": False},
            {"$set": {"is_read": True}}
        )

        history = list(messages_collection.find({
            "$or": [
                {"sender_id": u_id, "receiver_id": r_id},
                {"sender_id": r_id, "receiver_id": u_id}
            ]
        }).sort("timestamp", 1))

        return render_template('chat.html', receiver=rec, messages=history)
    except Exception as e:
        print(f"Chat Error: {e}")
        return redirect(url_for('messages_list'))


@app.route('/community/<community_id>')
@login_required
def view_community(community_id):

    community = communities_collection.find_one(
        {"_id": ObjectId(community_id)}
    )

    if not community:
        flash("Community not found", "error")
        return redirect(url_for("find_communities"))

    user_id = ObjectId(current_user.id)

    is_owner = community["owner_id"] == user_id
    is_admin = user_id in community.get("admins", [])
    is_member = user_id in community.get("members", [])

    # Fetch full member data
    member_users = list(
        users_collection.find(
            {"_id": {"$in": community.get("members", [])}},
            {"name": 1}
        )
    )

    members_data = []

    for user in member_users:

        role = "member"

        if user["_id"] == community["owner_id"]:
            role = "owner"
        elif user["_id"] in community.get("admins", []):
            role = "admin"

        members_data.append({
            "_id": str(user["_id"]),
            "name": user.get("name", "User"),
            "role": role
        })

    messages = list(
        community_messages_collection.find(
            {"community_id": ObjectId(community_id)}
        ).sort("timestamp", 1)
    )

    return render_template(
        "community_chat.html",
        community=community,
        messages=messages,
        members_data=members_data,
        is_owner=is_owner,
        is_admin=is_admin,
        is_member=is_member
    )





@app.route('/community/<community_id>/send', methods=['POST'])
@login_required
def send_community_message(community_id):

    msg = request.form.get("message", "").strip()

    if msg:
        community_messages_collection.insert_one({
            "community_id": ObjectId(community_id),
            "sender_id": ObjectId(current_user.id),
            "sender_name": current_user.name,
            "message": msg,
            "timestamp": datetime.utcnow(),
            "reactions": {}   # 🔥 important
        })

    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/react/<message_id>/<emoji>')
@login_required
def react_to_message(community_id, message_id, emoji):

    user_id = ObjectId(current_user.id)

    message = community_messages_collection.find_one(
        {"_id": ObjectId(message_id)}
    )

    if not message:
        return redirect(url_for("view_community", community_id=community_id))

    reactions = message.get("reactions", {})

    # If emoji not exist → create
    if emoji not in reactions:
        reactions[emoji] = []

    # Toggle logic
    if user_id in reactions[emoji]:
        reactions[emoji].remove(user_id)
    else:
        reactions[emoji].append(user_id)

    community_messages_collection.update_one(
        {"_id": ObjectId(message_id)},
        {"$set": {"reactions": reactions}}
    )

    return redirect(url_for("view_community", community_id=community_id))


@app.route('/community/<community_id>/request')
@login_required
def request_to_join_community(community_id):

    obj_id = ObjectId(community_id)
    user_id = ObjectId(current_user.id)

    community = communities_collection.find_one({"_id": obj_id})

    if not community:
        flash("Community not found.", "error")
        return redirect(url_for("find_communities"))

    if user_id in community.get("members", []):
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": obj_id},
        {
            "$addToSet": {"pending_requests": user_id},
            "$pull": {"rejected_requests": user_id}
        }
    )

    flash("Request sent! Wait for owner approval.", "info")
    return redirect(url_for("view_community", community_id=community_id))


@app.route('/community/<community_id>/approve/<user_id>')
@login_required
def approve_member(community_id, user_id):

    community = communities_collection.find_one(
        {"_id": ObjectId(community_id)}
    )

    if not community:
        flash("Community not found.", "error")
        return redirect(url_for("find_communities"))

    # Check if current user is owner or admin
    is_allowed = False
    for member in community.get("members", []):
        if member["user_id"] == ObjectId(current_user.id) and member["role"] in ["owner", "admin"]:
            is_allowed = True

    if not is_allowed:
        flash("Unauthorized action.", "error")
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": ObjectId(community_id)},
        {
            "$pull": {"pending_requests": ObjectId(user_id)},
            "$push": {
                "members": {
                    "user_id": ObjectId(user_id),
                    "role": "member"
                }
            }
        }
    )

    flash("Member approved!", "success")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/make_admin/<user_id>')
@login_required
def make_admin(community_id, user_id):

    community = communities_collection.find_one(
        {"_id": ObjectId(community_id)}
    )

    if community["owner_id"] != ObjectId(current_user.id):
        flash("Only owner can assign admin.", "error")
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": ObjectId(community_id)},
        {"$addToSet": {"admins": ObjectId(user_id)}}
    )

    flash("Member promoted to Admin.", "success")
    return redirect(url_for("view_community", community_id=community_id))


@app.route('/community/<community_id>/decline/<user_id>')
@login_required
def decline_member(community_id, user_id):

    obj_id = ObjectId(community_id)
    user_obj = ObjectId(user_id)

    community = communities_collection.find_one({"_id": obj_id})

    if community["owner_id"] != ObjectId(current_user.id):
        flash("Unauthorized.", "error")
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": obj_id},
        {
            "$pull": {"pending_requests": user_obj},
            "$addToSet": {"rejected_requests": user_obj}
        }
    )

    flash("Request declined.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/remove_member/<user_id>')
@login_required
def remove_member(community_id, user_id):

    community = communities_collection.find_one(
        {"_id": ObjectId(community_id)}
    )

    current_user_id = ObjectId(current_user.id)

    is_owner = community["owner_id"] == current_user_id
    is_admin = current_user_id in community.get("admins", [])

    if not is_owner and not is_admin:
        flash("Unauthorized action.", "error")
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": ObjectId(community_id)},
        {
            "$pull": {
                "members": ObjectId(user_id),
                "admins": ObjectId(user_id)
            }
        }
    )

    flash("Member removed.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/remove_admin/<user_id>')
@login_required
def remove_admin(community_id, user_id):

    community = communities_collection.find_one(
        {"_id": ObjectId(community_id)}
    )

    if community["owner_id"] != ObjectId(current_user.id):
        flash("Only owner can remove admin.", "error")
        return redirect(url_for("view_community", community_id=community_id))

    communities_collection.update_one(
        {"_id": ObjectId(community_id)},
        {"$pull": {"admins": ObjectId(user_id)}}
    )

    flash("Admin removed.", "info")
    return redirect(url_for("view_community", community_id=community_id))


@app.route('/communities')
@login_required
def find_communities():

    user_id = ObjectId(current_user.id)

    communities = list(
        communities_collection.find().sort("created_at", -1)
    )

    for c in communities:
        c["is_owner"] = c.get("owner_id") == user_id
        c["is_member"] = user_id in c.get("members", [])
        c["is_other"] = not c["is_owner"] and not c["is_member"]

    return render_template(
        "find_communities.html",
        communities=communities
    )



@app.route('/debug/communities')
@login_required
def debug_communities():
    data = list(communities_collection.find({}))
    return {
        "count": len(data),
        "data": [str(d) for d in data]
    }

print("CONNECTED DB NAME:", db.name)
print("COMMUNITIES COLLECTION:", communities_collection.full_name)

@app.route('/db-test-communities')
@login_required
def db_test_communities():
    before = communities_collection.count_documents({})
    
    communities_collection.insert_one({
        "test": "db_integration_check"
    })
    
    after = communities_collection.count_documents({})
    
    return {
        "before_insert": before,
        "after_insert": after,
        "collection": communities_collection.full_name
    }

@app.route('/force-community')
@login_required
def force_community():
    result = communities_collection.insert_one({
        "project_title": "FORCED COMMUNITY TEST",
        "skills_required": ["Python"],
        "visibility": "public",
        "owner_id": ObjectId(current_user.id),
        "owner_name": current_user.name,
        "members": [ObjectId(current_user.id)],
        "created_at": datetime.utcnow()
    })
    return f"Inserted community with id: {result.inserted_id}"

# --- Main Execution ---
if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'profile_pics'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_img'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_assets'), exist_ok=True) 
    print(f"Ensured upload folder exists at: {app.config['UPLOAD_FOLDER']}")
    app.run(debug=True)