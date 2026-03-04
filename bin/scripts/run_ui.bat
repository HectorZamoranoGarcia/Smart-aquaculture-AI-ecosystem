@echo off
REM =============================================================================
REM run_ui.bat — OceanTrust AI · Streamlit Control Tower launcher (Windows)
REM =============================================================================
REM Usage:
REM   bin\scripts\run_ui.bat
REM
REM Description:
REM   Sets PYTHONPATH to the project root so that all `src.*` module imports
REM   resolve correctly, then launches the Streamlit Control Tower dashboard.
REM
REM Environment variables (optional overrides — set before calling this script):
REM   STREAMLIT_SERVER_PORT  : HTTP port Streamlit listens on  (default: 8501)
REM   STREAMLIT_SERVER_HOST  : Bind host                       (default: localhost)
REM   GOOGLE_API_KEY         : Required for the RAG search panel
REM   QDRANT_HOST            : Qdrant hostname                  (default: localhost)
REM   QDRANT_PORT            : Qdrant REST port                 (default: 6333)
REM   KAFKA_BOOTSTRAP_SERVERS: Redpanda broker address          (default: localhost:19092)
REM =============================================================================

SETLOCAL ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION

REM ---------------------------------------------------------------------------
REM Resolve the project root (two levels up from this script's directory)
REM ---------------------------------------------------------------------------
SET "SCRIPT_DIR=%~dp0"
PUSHD "%SCRIPT_DIR%..\..\"
SET "PROJECT_ROOT=%CD%"
POPD

REM ---------------------------------------------------------------------------
REM Set PYTHONPATH so `src.*` imports resolve without an editable install
REM ---------------------------------------------------------------------------
SET "PYTHONPATH=%PROJECT_ROOT%"

REM ---------------------------------------------------------------------------
REM Default Kafka bootstrap to the external listener (host -> container)
REM ---------------------------------------------------------------------------
IF "%KAFKA_BOOTSTRAP_SERVERS%"=="" SET "KAFKA_BOOTSTRAP_SERVERS=localhost:19092"

REM ---------------------------------------------------------------------------
REM Default Streamlit port and host if not already set
REM ---------------------------------------------------------------------------
IF "%STREAMLIT_SERVER_PORT%"=="" SET "STREAMLIT_SERVER_PORT=8501"
IF "%STREAMLIT_SERVER_HOST%"=="" SET "STREAMLIT_SERVER_HOST=localhost"

ECHO [run_ui] Project root  : %PROJECT_ROOT%
ECHO [run_ui] PYTHONPATH    : %PYTHONPATH%
ECHO [run_ui] Kafka brokers : %KAFKA_BOOTSTRAP_SERVERS%
ECHO [run_ui] Launching Streamlit dashboard ...

streamlit run ^
    "%PROJECT_ROOT%\src\ui\dashboard.py" ^
    --server.port %STREAMLIT_SERVER_PORT% ^
    --server.address %STREAMLIT_SERVER_HOST% ^
    --server.headless true ^
    --browser.gatherUsageStats false

ENDLOCAL
