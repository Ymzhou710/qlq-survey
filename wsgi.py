# WSGI entry point for PythonAnywhere
# This file tells PythonAnywhere how to run the survey app

import sys
import os

# Set the path to your project directory
project_dir = os.path.dirname(os.path.abspath(__file__))
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

# Set environment for cloud deployment
os.environ["DATA_DIR"] = os.path.join(project_dir, "data")
os.environ["BASE_URL"] = "https://YOUR_USERNAME.pythonanywhere.com"  # 替换为你的用户名

# Import the Flask app
from survey_app import app as application
