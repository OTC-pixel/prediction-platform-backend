from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import subprocess
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def fetch_fixtures_job():
    print("📅 Running scheduled fetch_fixtures.py...")
    try:
        script_path = os.path.join(BASE_DIR, 'services', 'fetch_fixtures.py')
        result = subprocess.run([sys.executable, script_path], capture_output=True, text=True, check=True)
        print("✅ Fixtures fetch completed.")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to fetch fixtures:\n{e.stderr}")

def collect_and_process_results_job():
    print("🔁 Running scheduled collect_results.py...")
    try:
        script_path = os.path.join(BASE_DIR, 'services', 'collect_results.py')
        result = subprocess.run([sys.executable, script_path], capture_output=True, text=True, check=True)
        print("✅ Results collection and processing completed.")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to collect results:\n{e.stderr}")

def run_runner_job():
    print("🚀 Running scheduled runner.py...")
    try:
        script_path = os.path.join(BASE_DIR, 'runner.py')
        result = subprocess.run([sys.executable, script_path], capture_output=True, text=True, check=True)
        print("✅ Runner processing completed.")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to run runner.py:\n{e.stderr}")

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_fixtures_job, trigger="interval", hours=2)
    scheduler.add_job(collect_and_process_results_job, trigger="interval", hours=1)
    scheduler.add_job(run_runner_job, trigger="interval", hours=1)
    
    scheduler.start()
    print("⏰ Scheduler started: Fixtures every 2h, Results every 1h, Refreshing every 1h.")
    atexit.register(lambda: scheduler.shutdown())
