import streamlit as st
import os
import time

# --- Configuration and Setup ---
def load_config():
    """Loads configuration from Streamlit secrets."""
    config = {}
    try:
        # Example of loading an API key from st.secrets
        config['api_key'] = st.secrets.get("API_KEY", "default_api_key_if_not_found")
        config['app_name'] = st.secrets.get("APP_NAME", "My Streamlit App")
        config['default_value'] = st.secrets.get("DEFAULT_VALUE", 50)
        st.success("Configuration loaded successfully!")
    except Exception as e:
        st.error(f"Error loading configuration: {e}")
        config['api_key'] = os.getenv("API_KEY", "fallback_api_key") # Fallback to environment variable
        config['app_name'] = "My Streamlit App (Fallback)"
        config['default_value'] = 50
    return config

# Initialize session state variables if they don't exist
if 'input_text' not in st.session_state:
    st.session_state.input_text = "Hello Streamlit!"
if 'slider_value' not in st.session_state:
    st.session_state.slider_value = 50
if 'checkbox_state' not in st.session_state:
    st.session_state.checkbox_state = False
if 'processing_status' not in st.session_state:
    st.session_state.processing_status = "idle"

# Load application configuration
app_config = load_config()

# --- Page Configuration ---
st.set_page_config(
    page_title=app_config['app_name'],
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Sidebar Layout ---
def render_sidebar():
    """Renders the sidebar content for user controls and navigation."""
    st.sidebar.header("Application Controls")
    st.sidebar.markdown("---
**Navigation**")
    # Example navigation or feature selection
    selected_page = st.sidebar.radio(
        "Go to",
        ["Dashboard", "Settings", "About"],
        key='sidebar_navigation'
    )

    st.sidebar.markdown("---
**Configuration**")
    # Example user input in sidebar
    st.session_state.slider_value = st.sidebar.slider(
        "Adjust a value",
        min_value=0,
        max_value=100,
        value=app_config['default_value'],
        key='sidebar_slider'
    )
    st.session_state.checkbox_state = st.sidebar.checkbox(
        "Enable Feature X",
        value=False,
        key='sidebar_checkbox'
    )

    st.sidebar.markdown("---
API Key: `" + app_config['api_key'][:4] + "..." + app_config['api_key'][-4:] + "`")
    return selected_page

# --- Main Application Area Functions ---
def display_metric_cards():
    """Displays key metrics using st.metric."""
    st.subheader("Key Metrics")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Users", "1,234", "12%")
    with col2:
        st.metric("Revenue", "$12,345", "-8%")
    with col3:
        st.metric("Active Sessions", "123", "5%")

def interactive_widgets():
    """Renders interactive widgets and processes user input."""
    st.subheader("Interactive Input")
    st.session_state.input_text = st.text_input(
        "Enter some text",
        value=st.session_state.input_text,
        key='main_text_input'
    )

    st.write(f"You entered: **{st.session_state.input_text}**")
    st.write(f"Sidebar slider value: **{st.session_state.slider_value}**")
    st.write(f"Feature X enabled: **{st.session_state.checkbox_state}**")

    if st.button("Process Data"):
        try:
            st.session_state.processing_status = "processing"
            with st.spinner('Processing data...'):
                time.sleep(2) # Simulate a long running process
                result = f"Processed '{st.session_state.input_text}' with value {st.session_state.slider_value}"
                if st.session_state.checkbox_state:
                    result += " and Feature X enabled."
                st.session_state.last_result = result
                st.success("Data processing complete!")
            st.session_state.processing_status = "complete"
        except Exception as e:
            st.session_state.processing_status = "error"
            st.error(f"An error occurred during processing: {e}")

    if st.session_state.processing_status == "complete":
        st.info(f"Last processing result: {st.session_state.last_result}")
    elif st.session_state.processing_status == "error":
        st.error("Please check the input and try again.")

def display_feedback_messages():
    """Shows various types of feedback messages."""
    st.subheader("Feedback & Notifications")
    if st.session_state.checkbox_state:
        st.success("Feature X is currently active.")
    else:
        st.info("Enable Feature X in the sidebar to unlock more functionalities.")

    if len(st.session_state.input_text) < 5:
        st.warning("Input text is very short. Consider providing more details.")

# --- Main Application Logic ---
def main_app_logic():
    """Orchestrates the main content of the application based on selected page."""
    st.title(app_config['app_name'])

    selected_page = render_sidebar()

    st.markdown("---
")

    if selected_page == "Dashboard":
        display_metric_cards()
        st.markdown("---
")
        interactive_widgets()
        st.markdown("---
")
        display_feedback_messages()
    elif selected_page == "Settings":
        st.header("Application Settings")
        st.write("Here you can configure application-wide settings.")
        st.warning("Settings page is under development.")
        # Example of a setting controlled by session_state
        st.session_state.theme_toggle = st.checkbox("Dark Mode", key='dark_mode_setting')
        if st.session_state.theme_toggle:
            st.success("Dark mode enabled!")
        else:
            st.info("Light mode active.")
    elif selected_page == "About":
        st.header("About This App")
        st.markdown("""
        This is a demo Streamlit application showcasing best practices for deployment 
        on Streamlit Cloud. It includes:
        - Page configuration (`st.set_page_config`)
        - Modular functions and error handling
        - Secure configuration loading (`st.secrets`)
        - Clean sidebar layout
        - Interactive main area with metrics, widgets, and feedback
        - State management with `st.session_state`
        """)
        st.write(f"Version: 1.0.0")
        st.write("Developed by: AI Assistant")

# --- Run the application ---
if __name__ == "__main__":
    main_app_logic()
