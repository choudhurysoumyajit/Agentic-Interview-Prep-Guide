import os
import re
from urllib.parse import urlparse, parse_qs
import requests
from tavily import TavilyClient
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Configuration
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize Clients
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
TRANSCRIPT_API_URL = "https://youtube-transcript-api-tau-one.vercel.app/transcript"

def extract_video_id(url):
    """Extracts the 11-character YouTube video ID from various URL formats."""
    parsed_url = urlparse(url)
    hostname = (parsed_url.hostname or '').lower()
    if hostname in ('youtu.be', 'www.youtu.be'):
        return parsed_url.path[1:]
    if hostname in ('youtube.com', 'www.youtube.com'):
        if parsed_url.path == '/watch':
            return parse_qs(parsed_url.query).get('v', [None])[0]
        if parsed_url.path.startswith(('/embed/', '/v/')):
            return parsed_url.path.split('/')[2]
    return None

def get_videos_from_tavily(query_string, max_results=10):
    print(f"Searching YouTube videos via Tavily for: '{query_string}'...")
    
    # Restrict searches specifically to YouTube domains
    search_query = f"site:youtube.com {query_string}"
    
    response = tavily_client.search(
        query=search_query, 
        max_results=max_results * 2, # Fetch overhead in case links aren't standard watch videos
        include_answer=False
    )
    
    unique_videos = {}
    for result in response.get('results', []):
        url = result.get('url', '')
        title = result.get('title', '')
        video_id = extract_video_id(url)
        
        # Ensure we only pick valid watch links and avoid duplicate IDs
        if video_id and video_id not in unique_videos:
            unique_videos[video_id] = {
                'id': video_id,
                'title': title,
                'url': url
            }
            if len(unique_videos) >= max_results:
                break
                
    return list(unique_videos.values())

def fetch_transcript(video_id):
    try:
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=('en',))
        return " ".join(snippet.text for snippet in transcript)
    except (TranscriptsDisabled, NoTranscriptFound):
        pass
    except Exception as error:
        print(f"   Local transcript lookup failed for {video_id}: {error}")

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        response = requests.post(
            TRANSCRIPT_API_URL,
            json={"url": video_url},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("transcript")
    except requests.RequestException as error:
        print(f"   Hosted transcript fallback failed for {video_id}: {error}")
    except (TypeError, ValueError) as error:
        print(f"   Invalid hosted transcript response for {video_id}: {error}")
    return None

def analyze_transcripts_with_gemini(aggregated_text):
    print("Analyzing combined transcripts via Gemini AI...")
    
    prompt = f"""
    You are an expert technical interviewer specializing in Big Data engineering, PySpark, and Databricks. 
    Below is a compiled text transcript gathered from multiple Accenture Databricks interview preparation videos.
    
    Extract exactly 10 distinct, highly realistic technical interview questions mentioned or discussed in this text. 
    Format your response cleanly as a numbered list with the question bolded, followed by a 1-sentence context summary of why it's asked.
    
    Transcript Context:
    {aggregated_text[:15000]} 
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt
    )
    return response.text

# --- Execution Flow ---
if __name__ == "__main__":
    # Combined search term targeting your 3 core keywords
    target_query = "Accenture Databricks Interview"
    
    video_list = get_videos_from_tavily(target_query, max_results=10)
    
    print("\n--- Videos Found via Tavily Scraper ---")
    if video_list:
        print("\nVideo URLs:")
        for vid in video_list:
            print(vid['url'])

    combined_transcripts = ""
    
    for idx, vid in enumerate(video_list, 1):
        print(f"{idx}. {vid['title']} \n   URL: {vid['url']}")
        
        # Pull text from the video
        text = fetch_transcript(vid['id'])
        if text:
            combined_transcripts += f"\n[Video Context: {vid['title']}]\n" + text
        else:
            print(f"   (No public transcript available for this video layout)")

    if combined_transcripts.strip():
        # Step 3. Generate the 10 questions using Gemini 
        interview_questions = analyze_transcripts_with_gemini(combined_transcripts)
        print("\n--- Gemini Extracted 10 Interview Questions ---")
        print(interview_questions)
    else:
        print("\nCould not gather enough raw transcript text to compile questions. Try checking if the videos found have closed captioning available.")
