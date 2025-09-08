from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
import os, re, praw, time, threading
from elevenlabs import generate, set_api_key
import base64

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
reddit = None
current_submission = None
seen_comments = set()
is_streaming = False
comment_thread = None
current_settings = {
    'reddit_url': '',
    'voice_id': 'od84OdVweqzO3t6kKlWT',
}

def debug_print(message):
    if DEBUG:
        print(f"[DEBUG] {message}")
        socketio.emit('debug', {'message': message})

def initialize_reddit():
    global reddit
    try:
        # Get credentials from environment variables
        client_id = os.environ.get('REDDIT_CLIENT_ID')
        client_secret = os.environ.get('REDDIT_CLIENT_SECRET')
        
        # Check if credentials are provided
        if not client_id:
            debug_print("ERROR: REDDIT_CLIENT_ID environment variable not set")
            return False
            
        if not client_secret:
            debug_print("ERROR: REDDIT_CLIENT_SECRET environment variable not set")
            return False
        
        debug_print(f"Using Reddit client ID: {client_id[:6]}...")
        debug_print(f"Using Reddit client secret: {client_secret[:6]}...")
        
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="tts-streamer/1.0 by YourUsername",
        )
        
        # Test the connection by making a simple request
        reddit.user.me()
        
        debug_print("Reddit initialized successfully")
        return True
        
    except Exception as e:
        debug_print(f"ERROR connecting to Reddit API: {e}")
        debug_print(f"Error type: {type(e).__name__}")
        
        # Provide more specific error information
        if "401" in str(e):
            debug_print("Authentication failed - check your Reddit API credentials")
        elif "403" in str(e):
            debug_print("Access forbidden - your app may not have the right permissions")
        
        return False

def initialize_elevenlabs():
    try:
        eleven_api_key = os.environ.get('ELEVENLABS_API_KEY')
        
        if not eleven_api_key:
            debug_print("ERROR: ELEVENLABS_API_KEY environment variable not set")
            return False
            
        debug_print(f"Using ElevenLabs API key: {eleven_api_key[:6]}...")
        
        set_api_key(eleven_api_key)
        debug_print("ElevenLabs initialized successfully")
        return True
        
    except Exception as e:
        debug_print(f"ERROR connecting to ElevenLabs API: {e}")
        return False

def clean_text(markdown: str) -> str:
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", markdown)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def speak_text(text: str, voice_id: str):
    if not text.strip() or len(text.strip()) < 3:
        return None
    
    try:
        cleaned_text = clean_text(text)
        if len(cleaned_text) > 500:
            cleaned_text = cleaned_text[:497] + "..."
        
        debug_print(f"Converting text to speech: {cleaned_text[:50]}...")
        
        audio = generate(
            text=cleaned_text,
            voice=voice_id,
            model="eleven_monolingual_v1"
        )
        
        return audio
        
    except Exception as e:
        debug_print(f"ERROR in speak_text function: {e}")
        return None

def comment_monitor():
    global is_streaming, current_submission, seen_comments
    
    if not current_submission:
        return
    
    submission_id = current_submission.id
    
    try:
        last_comment_time = 0
        min_interval = 3
        
        for comment in reddit.subreddit(current_submission.subreddit.display_name).stream.comments(skip_existing=True):
            if not is_streaming:
                break
            
            current_time = time.time()
            if current_time - last_comment_time < min_interval:
                continue
                
            if comment.link_id.split("_")[1] != submission_id:
                continue

            if comment.body in ['[deleted]', '[removed]', '']:
                continue

            if comment.id not in seen_comments and len(comment.body.strip()) > 5:
                seen_comments.add(comment.id)
                last_comment_time = current_time
                
                comment_data = {
                    'id': comment.id,
                    'author': str(comment.author) if comment.author else '[deleted]',
                    'body': comment.body[:500],
                    'timestamp': time.time(),
                    'permalink': comment.permalink
                }
                
                debug_print(f"New comment by {comment_data['author']}: {comment_data['body'][:80]}")
                socketio.emit('new_comment', comment_data)
                
                audio_data = speak_text(comment.body, current_settings['voice_id'])
                if audio_data:
                    audio_b64 = base64.b64encode(audio_data).decode('utf-8')
                    socketio.emit('play_audio', {
                        'audio': audio_b64,
                        'comment_id': comment.id,
                        'text': comment.body[:100]
                    })
                
    except Exception as e:
        debug_print(f"ERROR in comment stream: {e}")
        socketio.emit('error', {'message': f'Comment stream error: {str(e)}'})

@app.route('/')
def index():
    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return "<h1>Reddit TTS Stream</h1><p>index.html not found</p>"

@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy', 
        'reddit_connected': reddit is not None,
        'env_vars': {
            'REDDIT_CLIENT_ID': os.environ.get('REDDIT_CLIENT_ID') is not None,
            'REDDIT_CLIENT_SECRET': os.environ.get('REDDIT_CLIENT_SECRET') is not None,
            'ELEVENLABS_API_KEY': os.environ.get('ELEVENLABS_API_KEY') is not None
        }
    })

@app.route('/api/start_stream', methods=['POST'])
def start_stream():
    global is_streaming, current_submission, comment_thread, seen_comments
    
    # Check if Reddit is connected
    if reddit is None:
        return jsonify({'error': 'Reddit API not connected. Check server logs.'}), 500
    
    data = request.json
    reddit_url = data.get('reddit_url', '')
    
    if not reddit_url:
        return jsonify({'error': 'Reddit URL is required'}), 400
    
    if is_streaming:
        return jsonify({'error': 'Already streaming'}), 400
    
    try:
        if 'reddit.com' not in reddit_url:
            return jsonify({'error': 'Invalid Reddit URL'}), 400
        
        debug_print(f"Attempting to get submission from URL: {reddit_url}")
        current_submission = reddit.submission(url=reddit_url)
        current_submission.comments.replace_more(limit=0)
        
        # Test if we can access the submission
        title = current_submission.title
        debug_print(f"Successfully got submission: {title}")
        
        current_settings.update(data)
        seen_comments.clear()
        
        is_streaming = True
        comment_thread = threading.Thread(target=comment_monitor, daemon=True)
        comment_thread.start()
        
        return jsonify({
            'success': True,
            'title': title[:100],
            'subreddit': current_submission.subreddit.display_name,
            'author': str(current_submission.author) if current_submission.author else '[deleted]'
        })
        
    except Exception as e:
        debug_print(f"ERROR starting stream: {e}")
        debug_print(f"Error type: {type(e).__name__}")
        
        # Provide more specific error information
        if "404" in str(e):
            error_msg = "Reddit post not found. Check the URL."
        elif "403" in str(e):
            error_msg = "Access forbidden. The post may be private or deleted."
        elif "401" in str(e):
            error_msg = "Authentication failed. Check Reddit API credentials."
        else:
            error_msg = f'Failed to start stream: {str(e)}'
            
        return jsonify({'error': error_msg}), 500

@app.route('/api/stop_stream', methods=['POST'])
def stop_stream():
    global is_streaming, current_submission, comment_thread
    
    is_streaming = False
    current_submission = None
    
    if comment_thread and comment_thread.is_alive():
        comment_thread.join(timeout=1.0)
    
    return jsonify({'success': True})

@app.route('/api/test_audio', methods=['POST'])
def test_audio():
    try:
        test_text = "Text to speech system is working correctly."
        audio_data = speak_text(test_text, current_settings['voice_id'])
        
        if audio_data:
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            return jsonify({
                'success': True,
                'audio': audio_b64,
                'text': test_text
            })
        else:
            return jsonify({'error': 'Failed to generate audio'}), 500
            
    except Exception as e:
        return jsonify({'error': f'Audio test failed: {str(e)}'}), 500

@app.route('/api/settings', methods=['POST'])
def save_settings():
    global current_settings
    try:
        data = request.json
        current_settings.update(data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f'Failed to save settings: {str(e)}'}), 500

@socketio.on('connect')
def handle_connect():
    debug_print('Client connected')
    if reddit is None:
        emit('error', {'message': 'Reddit API not connected. Check server configuration.'})
    else:
        emit('status', {'message': 'Connected to server'})

@socketio.on('disconnect')
def handle_disconnect():
    debug_print('Client disconnected')

if __name__ == '__main__':
    print("🚀 Initializing Reddit TTS Web Application...")
    
    # Check environment variables first
    print("📋 Checking environment variables...")
    required_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'ELEVENLABS_API_KEY']
    missing_vars = []
    
    for var in required_vars:
        if not os.environ.get(var):
            missing_vars.append(var)
            print(f"❌ {var} not set")
        else:
            print(f"✅ {var} is set")
    
    if missing_vars:
        print(f"⚠️  Missing environment variables: {', '.join(missing_vars)}")
        print("⚠️  The app will start but Reddit/ElevenLabs features may not work")
    
    reddit_success = initialize_reddit()
    elevenlabs_success = initialize_elevenlabs()
    
    if not reddit_success:
        print("❌ Failed to initialize Reddit API")
    else:
        print("✅ Reddit API initialized successfully")
        
    if not elevenlabs_success:
        print("❌ Failed to initialize ElevenLabs API")
    else:
        print("✅ ElevenLabs API initialized successfully")
    
    print("🎧 Starting web server...")
    
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=DEBUG, host='0.0.0.0', port=port)
