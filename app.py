"""app.py — the entire UI. All logic lives in backend.py; this file just calls run_pipeline()."""
import os
import base64
from pathlib import Path
import streamlit as st
from streamlit.components.v1 import html
import backend

st.set_page_config(page_title="Interview Prep Generator", page_icon="🎯", layout="wide")
background_path = Path(__file__).with_name("background.avif")
background_data = (
    base64.b64encode(background_path.read_bytes()).decode()
    if background_path.exists() else ""
)
st.markdown(
    f"""
    <style>
    .stApp {{
        background-image: linear-gradient(rgba(8, 18, 38, 0.78), rgba(8, 18, 38, 0.88)),
            url('data:image/avif;base64,{background_data}');
        background-size: cover;
        background-attachment: fixed;
    }}
    .stApp, .stApp p, .stApp label, .stApp [data-testid="stMarkdownContainer"] {{
        color: #FFFFFF;
    }}
    .stApp h1 {{ color: #FFD166; }}
    [data-testid="stSidebar"] {{ background: rgba(8, 18, 38, 0.88); }}
    [data-testid="stSidebar"] * {{ color: #FFFFFF !important; }}
    .stTextInput input, .stNumberInput input {{ color: #FFFFFF !important; }}
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("🎯 AI Interview Question Generator")
st.caption(
    "Multi-agent research across the web and YouTube — consolidated into a "
    "downloadable Word document with a diagram or image for every question."
)

try:
    backend.validate_config()
except EnvironmentError as e:
    st.error(str(e))
    st.stop()

if "output_path" not in st.session_state:
    st.session_state.output_path = None
if "analysis_running" not in st.session_state:
    st.session_state.analysis_running = False
if "analysis_requested" not in st.session_state:
    st.session_state.analysis_requested = False


def reset_app_state():
    st.session_state.companies_raw = ""
    st.session_state.technologies_raw = ""
    st.session_state.num_pages = 5
    st.session_state.max_questions = 20
    st.session_state.output_path = None
    st.session_state.analysis_running = False
    st.session_state.analysis_requested = False
    deleted = backend.reset_generated_outputs()
    st.session_state.reset_message = f"Reset complete. Deleted {deleted} generated output(s)."


if st.query_params.get("refresh_reset"):
    reset_app_state()
    st.query_params.clear()
    st.rerun()


html(
    """
    <script>
    const navigation = performance.getEntriesByType("navigation")[0];
    const currentLoad = String(performance.timeOrigin);
    const storage = window.parent.sessionStorage;
    const previousLoad = storage.getItem("interview-prep-load");
    const url = new URL(window.parent.location.href);

    if (previousLoad !== currentLoad) {
        storage.setItem("interview-prep-load", currentLoad);
    }
    if (navigation && navigation.type === "reload" && previousLoad !== currentLoad
            && !url.searchParams.has("refresh_reset")) {
        url.searchParams.set("refresh_reset", currentLoad);
        window.parent.location.replace(url.toString());
    }
    </script>
    """,
    height=0,
)


def reset_inputs():
    reset_app_state()


def request_analysis():
    st.session_state.analysis_running = True
    st.session_state.analysis_requested = True

with st.sidebar:
    st.header("Inputs")
    companies_raw = st.text_input(
        "Organization Name(s) (optional)", placeholder="Leave blank for general interview questions",
        help="Comma-separated — leave blank to research the technology generally.",
        key="companies_raw",
    )
    technologies_raw = st.text_input(
        "Technologies", placeholder="e.g. Python, Kubernetes, React",
        help="Comma-separated technologies to focus the research on.",
        key="technologies_raw",
    )
    num_pages = st.number_input(
        "Webpages and YouTube links per technology (N)",
        min_value=1, max_value=15, value=5, step=1,
        help="Researches up to N non-YouTube webpages and N YouTube links separately.",
        key="num_pages",
    )
    max_questions = st.number_input(
        "Maximum questions in document",
        min_value=1, max_value=100, value=20, step=1,
        help="Questions are ranked by importance, then only the top questions are included.",
        key="max_questions",
    )
    st.divider()
    run_button = st.button(
        "⏳ Analysis in progress..." if st.session_state.analysis_running else "🚀 Generate Interview Guide",
        type="primary", use_container_width=True,
        disabled=st.session_state.analysis_running,
        on_click=request_analysis,
    )
    reset_button = st.button("↺ Reset inputs and visuals", on_click=reset_inputs, use_container_width=True)
    st.caption("⏱️ More companies/technologies and larger N take longer — each combination triggers its own research + extraction.")

if st.session_state.get("reset_message"):
    st.info(st.session_state.pop("reset_message"))

STAGE_PROGRESS = {
    "web_research": 15, "youtube_research": 30, "consolidate": 60,
    "visualize": 80, "build_document": 95,
}

WORKFLOW_STAGES = [
    ("web_research", "Web research", "&#128269;"),
    ("youtube_research", "YouTube research", "&#9654;"),
    ("consolidate", "Consolidate Q&A", "&#128221;"),
    ("visualize", "Create visuals", "&#128444;"),
    ("build_document", "Build document", "&#128196;"),
]


def render_workflow(completed=None, active=None):
    completed = completed or set()
    cards = []
    for index, (stage_id, label, icon) in enumerate(WORKFLOW_STAGES):
        if stage_id in completed:
            state_class, marker, status = "done", "&#10003;", "Completed"
        elif stage_id == active:
            state_class, marker, status = "active", icon, "In progress"
        else:
            state_class, marker, status = "pending", icon, "Pending"
        cards.append(
            f'<div class="workflow-step {state_class}">'
            f'<div class="workflow-marker">{marker}</div>'
            f'<div class="workflow-label">{label}</div>'
            f'<div class="workflow-status">{status}</div>'
            "</div>"
        )
        if index < len(WORKFLOW_STAGES) - 1:
            cards.append('<div class="workflow-arrow">&#8594;</div>')
    st.markdown(
        """
        <style>
        .workflow { display:flex; align-items:center; gap:8px; margin:12px 0 20px; }
        .workflow-step { flex:1; min-width:115px; padding:12px 10px; border:1px solid #4169e1;
            border-radius:8px; text-align:center; background:#4169e1; color:white; }
        .workflow-step.active { background:#4169e1; box-shadow:0 0 0 2px #9db4ff; }
        .workflow-step.done { background:#4169e1; }
        .workflow-marker { width:26px; height:26px; margin:0 auto 6px; border-radius:50%;
            display:flex; align-items:center; justify-content:center; background:#2948a8; color:white; font-weight:700; }
        .workflow-step.active .workflow-marker { background:#2948a8; }
        .workflow-step.done .workflow-marker { background:#16803c; color:white; }
        .workflow-label { font-size:0.85rem; font-weight:600; }
        .workflow-status { color:white; font-size:0.72rem; margin-top:3px; }
        .workflow-arrow { color:#4169e1; font-size:1.2rem; }
        @media (max-width: 800px) { .workflow { flex-direction:column; align-items:stretch; }
            .workflow-arrow { transform:rotate(90deg); text-align:center; } }
        </style>
        <div class="workflow">
        """ + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )

if st.session_state.analysis_requested:
    st.session_state.analysis_requested = False
    companies = [c.strip() for c in companies_raw.split(",") if c.strip()]
    technologies = [t.strip() for t in technologies_raw.split(",") if t.strip()]

    if not technologies:
        st.session_state.analysis_running = False
        st.error("Please provide at least one technology.")
    else:
        if not companies:
            companies = [""]
        progress = st.progress(0, text="Starting multi-agent research...")
        log_box = st.empty()
        workflow_box = st.empty()
        completed_stages = set()
        workflow_box.markdown("", unsafe_allow_html=True)
        with workflow_box.container():
            render_workflow()

        def on_update(node_name, node_partial):
            completed_stages.add(node_name)
            with workflow_box.container():
                render_workflow(completed=completed_stages)
            pct = STAGE_PROGRESS.get(node_name, 50)
            progress.progress(pct, text=f"Running: {node_name.replace('_', ' ').title()}...")
            recent_logs = node_partial.get("log", [])
            if recent_logs:
                log_box.code("\n".join(recent_logs[-10:]))

        try:
            with st.spinner("Agents are working..."):
                final_state = backend.run_pipeline(
                    companies, technologies, int(num_pages), on_update=on_update,
                    max_questions=int(max_questions),
                )
            progress.progress(100, text="Done!")

            output_path = final_state.get("output_path")
            if not output_path:
                st.warning(
                    "No resources could be identified. Try another company, technology, "
                    "or a generalized search."
                )
            else:
                st.session_state.output_path = output_path
                final_results = final_state.get("final_results", [])
                web_count = sum(len(cr.get("web_qas", [])) for cr in final_results)
                youtube_count = sum(len(cr.get("youtube_qas", [])) for cr in final_results)
                selected_count = sum(len(cr.get("consolidated_qas", [])) for cr in final_results)
                generated_images = sum(
                    bool(qa.get("visual_path"))
                    for cr in final_results
                    for qa in cr.get("consolidated_qas", [])
                )
                remaining_images = selected_count - generated_images
                st.info(
                    f"Questions identified: {web_count} from web and {youtube_count} from YouTube. "
                    f"Included in document: {selected_count}. "
                    f"Images generated: {generated_images}; remaining: {remaining_images}."
                )
                st.success(f"Document generated for {len(companies)} companie(s) and {len(technologies)} technology(ies).")
        except backend.PipelineCancelled:
            st.warning("Analysis cancelled by reset or page refresh. No document was generated.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Something went wrong: {e}")
        finally:
            st.session_state.analysis_running = False

if st.session_state.output_path and os.path.exists(st.session_state.output_path):
    st.divider()
    with open(st.session_state.output_path, "rb") as f:
        st.download_button(
            "⬇️ Download Word Document", data=f.read(),
            file_name=os.path.basename(st.session_state.output_path),
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )
