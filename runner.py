import os
import sys
from services.predictions import process_and_evaluate_latest_matchday

# Ensure script works regardless of where it's run from
current_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_dir)

print(" Starting results processing...")

# Step 1: Process and evaluate predictions based on stored results
process_and_evaluate_latest_matchday()

print(" Results processing completed.")
