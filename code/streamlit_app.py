"""Entry point for a Code Ocean Streamlit Cloud Workstation, which serves code/streamlit_app.py.
The app itself is app.py (also runnable directly: streamlit run app.py)."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("app.py")), run_name="__main__")
