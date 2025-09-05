from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import os, re, praw, time, queue, threading, json
from elevenlabs.client import ElevenLabs
import sys
import numpy as np
import base64
from io import BytesIO

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Global variables
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
reddit = None
tts = None
current_submission = None
seen_comments = set()
is_streaming = False
comment_thread = None
audio_queue = queue.Queue()
current_settings = {
    'reddit_url': '',
    'voice_id': 'od84OdVweqzO3t6kKlWT',
    'stability': 0.71,
    'similarity_boost': 0.5,
    'style': 0.0,
    'use_speaker_boost': True
}

def debug_print(message):
    if DEBUG:
        print(f"[DEBUG] {message}")
        socketio.emit('debug', {'message': message})

def initialize_reddit():
    global reddit
    try:
        # Use environment variables for production
        client_id = os.environ.get('REDDIT_CLIENT_ID', 'ZWbGdR3jT0TjpRPIlKWMAA')
        client_secret = os.environ.get('REDDIT_CLIENT_SECRET', 'MjoRxAeGZfOV9FaHht9i2hu5iFYcjg')
        
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="tts-streamer/1.0 by DirtPuzzleheaded5521",
        )
        
        # Test the connection
        reddit.user.me()
        debug_print(f"Reddit initialized successfully")
        return True
    except Exception as e:
        debug_print(f"ERROR connecting to Reddit API: {e}")
        return False

def initialize_elevenlabs():
    global tts
    try:
        # Use environment variable for production
        eleven_api_key = os.environ.get('ELEVENLABS_API_KEY', 'sk_328ed0b28661215a7331caa5029cfd0201a057920c654560')
        tts = ElevenLabs(api_key=eleven_api_key)
        debug_print("ElevenLabs initialized successfully")
        return True
    except Exception as e:
        debug_print(f"ERROR connecting to ElevenLabs API: {e}")
        return False

def clean_text(markdown: str) -> str:
    # Remove markdown links
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", markdown)
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def speak_text(text: str, voice_id: str) -> bytes:
    if not text.strip() or len(text.strip()) < 3:
        return None
    
    try:
        cleaned_text = clean_text(text)
        
        # Truncate very long text
        if len(cleaned_text) > 500:
            cleaned_text = cleaned_text[:497] + "..."
        
        debug_print(f"Converting text to speech: {cleaned_text[:50]}...")
        
        # Generate audio using ElevenLabs
        audio_generator = tts.text_to_speech.convert_as_stream(
            voice_id=voice_id,
            text=cleaned_text,
            output_format="mp3_22050_32",
            voice_settings={
                "stability": current_settings['stability'],
                "similarity_boost": current_settings['similarity_boost'],
                "style": current_settings['style'],
                "use_speaker_boost": current_settings['use_speaker_boost']
            }
        )
        
        # Collect all audio chunks
        audio_data = BytesIO()
        for chunk in audio_generator:
            audio_data.write(chunk)
        
        return audio_data.getvalue()
        
    except Exception as e:
        debug_print(f"ERROR in speak_text function: {e}")
        return None

def comment_monitor():
    global is_streaming, current_submission, seen_comments
    
    if not current_submission:
        return
    
    submission_id = current_submission.id
    
    try:
        # Add rate limiting
        last_comment_time = 0
        min_interval = 2  # Minimum 2 seconds between comments
        
        for comment in reddit.subreddit(current_submission.subreddit.display_name).stream.comments(skip_existing=True):
            if not is_streaming:
                break
            
            # Rate limiting
            current_time = time.time()
            if current_time - last_comment_time < min_interval:
                continue
                
            # Only process comments from the target submission
            if comment.link_id.split("_")[1] != submission_id:
                continue

            # Skip deleted/removed comments
            if comment.body in ['[deleted]', '[removed]', '']:
                continue

            if comment.id not in seen_comments and len(comment.body.strip()) > 5:
                seen_comments.add(comment.id)
                last_comment_time = current_time
                
                comment_data = {
                    'id': comment.id,
                    'author': str(comment.author) if comment.author else '[deleted]',
                    'body': comment.body[:500],  # Truncate long comments
                    'timestamp': time.time(),
                    'permalink': comment.permalink
                }
                
                debug_print(f"New comment by {comment_data['author']}: {comment_data['body'][:80]}")
                
                # Emit comment to frontend
                socketio.emit('new_comment', comment_data)
                
                # Generate and emit audio
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
    return render_template('index.html')

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'reddit': reddit is not None, 'tts': tts is not None})

@app.route('/api/start_stream', methods=['POST'])
def start_stream():
    global is_streaming, current_submission, comment_thread, seen_comments
    
    data = request.json
    reddit_url = data.get('reddit_url', '')
    
    if not reddit_url:
        return jsonify({'error': 'Reddit URL is required'}), 400
    
    if is_streaming:
        return jsonify({'error': 'Already streaming'}), 400
    
    try:
        # Validate Reddit URL
        if 'reddit.com' not in reddit_url:
            return jsonify({'error': 'Invalid Reddit URL'}), 400
        
        # Load the Reddit submission
        current_submission = reddit.submission(url=reddit_url)
        current_submission.comments.replace_more(limit=0)  # Load comments
        
        # Update settings
        current_settings.update(data)
        
        # Reset seen comments
        seen_comments.clear()
        
        # Start streaming
        is_streaming = True
        comment_thread = threading.Thread(target=comment_monitor, daemon=True)
        comment_thread.start()
        
        return jsonify({
            'success': True,
            'title': current_submission.title[:100],  # Truncate long titles
            'subreddit': current_submission.subreddit.display_name,
            'author': str(current_submission.author) if current_submission.author else '[deleted]'
        })
        
    except Exception as e:
        debug_print(f"ERROR starting stream: {e}")
        return jsonify({'error': f'Failed to start stream: {str(e)}'}), 500

@app.route('/api/stop_stream', methods=['POST'])
def stop_stream():
    global is_streaming, current_submission, comment_thread
    
    is_streaming = False
    current_submission = None
    
    if comment_thread and comment_thread.is_alive():
        comment_thread.join(timeout=1.0)
    
    return jsonify({'success': True})

@app.route('/api/settings', methods=['GET', 'POST'])
def handle_settings():
    global current_settings
    
    if request.method == 'GET':
        return jsonify(current_settings)
    
    if request.method == 'POST':
        data = request.json
        current_settings.update(data)
        return jsonify({'success': True, 'settings': current_settings})

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

@socketio.on('connect')
def handle_connect():
    debug_print('Client connected')
    emit('status', {'message': 'Connected to server'})

@socketio.on('disconnect')
def handle_disconnect():
    debug_print('Client disconnected')

def create_templates():
    """Create templates directory and copy HTML file for production"""
    import shutil
    os.makedirs('templates', exist_ok=True)
    if os.path.exists('index.html'):
        shutil.copy('index.html', 'templates/index.html')

if __name__ == '__main__':
    print("🚀 Initializing Reddit TTS Web Application...")
    
    # Initialize APIs
    if not initialize_reddit():
        print("❌ Failed to initialize Reddit API")
        sys.exit(1)
    
    if not initialize_elevenlabs():
        print("❌ Failed to initialize ElevenLabs API")
        sys.exit(1)
    
    print("✅ All APIs initialized successfully")
    
    # Create templates for production
    create_templates()
    
    print("🎧 Starting web server...")
    print("🌐 Web interface ready")
    
    # Use eventlet for production
    socketio.run(app, debug=DEBUG, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))