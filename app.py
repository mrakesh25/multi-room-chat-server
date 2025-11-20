from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit
from models import db, User, Message, Room
from datetime import datetime
import time, uuid, os
from functools import wraps
import emoji
import google.generativeai as genai
import traceback
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

socketio = SocketIO(app, cors_allowed_origins="*")
db.init_app(app)

# Create database tables
with app.app_context():
    db.create_all()

# Dictionary to store online users and chat history
online_users = {}  # Store users by room
user_rooms = {}    # Track which rooms each user is in (user -> set of rooms)
active_room = {}   # Track user's currently active room for messaging
chat_history = {}  # Store messages by room

# Initialize AI model
ai_model = None

def init_gemini():
    """Initialize the Gemini AI model"""
    try:
        # Configure the API key
        api_key = os.getenv('AIzaSyCLEAXjjo0WBLMaBwHO-idX6SgR9mByAN0')  # Get API key from environment variable
        if not api_key:
            print("No Gemini API key found in environment variables")
            return None
            
        genai.configure(api_key=api_key)
        
        # Set up the model and validate it works
        model = genai.GenerativeModel('gemini-pro')
        # Test the model with a simple prompt
        response = model.generate_content("Hello")
        if response and response.text:
            print("Gemini AI model initialized and tested successfully")
            return model
        else:
            print("Gemini AI model initialization test failed")
            return None
    except Exception as e:
        print(f"Error initializing Gemini AI: {e}")
        print("AI features will be disabled. The chat will work with basic responses only.")
        return None

# Initialize the AI model
ai_model = init_gemini()

def get_ai_response(message):
    """Generate AI response using Gemini or fallback to basic responses"""
    global ai_model  # Declare global at the start of the function
    
    try:
        # Check if AI model is available and properly initialized
        if ai_model is None:
            print("AI model not initialized, reinitializing...")
            ai_model = init_gemini()
            
        if ai_model is None:
            print("AI model initialization failed, using fallback responses")
            # Fallback responses if AI is not available
            responses = {
                "hello": "Hello! I'm your chat assistant. How can I help you today?",
                "hi": "Hi there! What can I do for you?",
                "help": "I can help you with:\n- Chat room navigation\n- General questions\n- Basic assistance\nJust let me know what you need!",
                "time": f"The current time is {datetime.now().strftime('%H:%M')}",
                "default": [
                    "I'm here to help! What would you like to know?",
                    "How can I assist you today?",
                    "Feel free to ask me anything!",
                    "I'm listening! What's on your mind?"
                ]
            }
            
            message_lower = message.lower()
            for key, response in responses.items():
                if key in message_lower:
                    return response if isinstance(response, str) else random.choice(response)
            return random.choice(responses["default"])
        
        # If AI model is available, use it with proper error handling
        try:
            generation_config = {
                "temperature": 0.7,
                "top_k": 40,
                "top_p": 0.8,
                "max_output_tokens": 200,
                "candidate_count": 1
            }
            
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            ]
            
            response = ai_model.generate_content(
                message,
                generation_config=generation_config,
                safety_settings=safety_settings
            )
            
            if response and hasattr(response, 'text') and response.text:
                text = response.text.strip()
                if len(text) > 500:
                    text = text[:497] + "..."
                return text
                
            print("Empty response from AI model")
            return "I understand your message but need a moment. Could you try rephrasing?"
            
        except Exception as inner_e:
            print(f"Error with Gemini response: {inner_e}")
            traceback.print_exc()
            # Try reinitializing the model (global already declared at function start)
            ai_model = init_gemini()
            return "I'm currently facing some technical issues. Please try again in a moment."
                
    except Exception as e:
        print(f"Error in get_ai_response: {e}")
        traceback.print_exc()
        return "I'm experiencing technical difficulties. Please try again later."

# User authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@login_required
def index():
    rooms = Room.query.all()
    return render_template('index.html', 
                         rooms=rooms, 
                         username=session.get('username'),
                         user_id=session.get('user_id'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = username
            return redirect(url_for('index'))
        
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            return redirect(url_for('register'))
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    username = session.get('username')
    if username:
        # Clean up user's room data
        if username in user_rooms:
            # Leave all rooms user was in
            for room in user_rooms[username]:
                if room in online_users and username in online_users[room]:
                    online_users[room].remove(username)
                leave_room(room)
            user_rooms.pop(username, None)
            active_room.pop(username, None)
            
    session.clear()
    return redirect(url_for('login'))

# Socket event handlers
@socketio.on('join')
def on_join(data):
    if 'user_id' not in session:
        return
    
    username = session['username']
    new_room = data.get('room')
    
    if not new_room:
        return
    
    try:
        # Initialize user's room set if needed
        if username not in user_rooms:
            user_rooms[username] = set()
            
        # Check if user is already in this room
        if new_room in user_rooms[username]:
            # Just make this the active room
            active_room[username] = new_room
            # Send chat history
            if new_room in chat_history:
                socketio.emit('chat_history', {'history': chat_history[new_room]}, room=request.sid)
            return
            
        # Join new room (no need to leave other rooms)
        join_room(new_room)
        user_rooms[username].add(new_room)
        active_room[username] = new_room  # Set as active room
        
        # Add user to online users for new room
        if new_room not in online_users:
            online_users[new_room] = set()
        online_users[new_room].add(username)
        
        # Initialize chat history for room if not exists
        if new_room not in chat_history:
            chat_history[new_room] = []
            
        # Send just the current chat history to user first
        if new_room in chat_history:
            socketio.emit('chat_history', {'history': chat_history[new_room]}, room=request.sid)
            
        # Send join message and update history - emit to everyone in room
        join_msg = {
            'id': str(uuid.uuid4()),
            'msg': f'{username} has joined the room.',
            'username': 'system',
            'system': True,
            'ts': int(time.time() * 1000)
        }
        socketio.emit('message', join_msg, to=new_room)
        chat_history[new_room].append(join_msg)
        
        # Update user list
        socketio.emit('update_users', {'users': list(online_users[new_room])}, to=new_room)
    
    except Exception as e:
        print(f"Error in room join: {e}")
        traceback.print_exc()

@socketio.on('leave')
def on_leave(data):
    if 'user_id' not in session:
        return
        
    username = session['username']
    room = data.get('room')
    
    if not room:
        return
        
    try:
        # Check if user is actually in this room
        if username not in user_rooms or user_rooms[username] != room:
            return
            
        # Handle actual leave (not room switch)
        leave_room(room)
        if room in online_users and username in online_users[room]:
            online_users[room].remove(username)
            # Send leave message and update history
            leave_msg = {
                'id': str(uuid.uuid4()),
                'msg': f'{username} has left the room.',
                'username': 'system',
                'system': True,
                'ts': int(time.time() * 1000)
            }
            socketio.emit('message', leave_msg, to=room)
            if room in chat_history:
                chat_history[room].append(leave_msg)
            # Update user list
            socketio.emit('update_users', {'users': list(online_users[room])}, to=room)
        
        # Remove user from room tracking
        user_rooms.pop(username, None)
            
    except Exception as e:
        print(f"Error in room leave: {e}")
        traceback.print_exc()

@socketio.on('message')
def handle_message(data):
    try:
        if 'user_id' not in session:
            return
        
        username = session.get('username')
        room = data.get('room')
        message_text = data.get('msg')
        
        if not username or not room or not message_text:
            return
        
        # Create message payload
        message_payload = {
            'id': str(uuid.uuid4()),
            'msg': f'{username}: {message_text}',
            'username': username,
            'system': False,
            'ts': int(time.time() * 1000)
        }
        
        # Store in chat history and emit to room
        if room not in chat_history:
            chat_history[room] = []
        chat_history[room].append(message_payload)
        socketio.emit('message', message_payload, to=room)
        
        # Only get AI response if message starts with "AI" or "@AI"
        if message_text.strip().lower().startswith(("ai", "@ai")):
            # Remove the "AI" or "@AI" prefix from the message
            actual_message = message_text.split(" ", 1)[1] if " " in message_text else ""
            if actual_message:
                ai_response = get_ai_response(actual_message)
                if ai_response:
                    ai_payload = {
                        'id': str(uuid.uuid4()),
                        'msg': f'AI Assistant: {ai_response}',
                        'username': 'AI Assistant',
                        'system': True,
                        'ts': int(time.time() * 1000)
                    }
                    chat_history[room].append(ai_payload)
                    socketio.emit('message', ai_payload, to=room)
    except Exception as e:
        print(f"Error in message handler: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"Error handling AI response: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    socketio.run(app, debug=True)