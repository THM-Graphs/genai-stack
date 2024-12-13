#!/bin/sh

# Default values for environment variables
: "${STREAMLIT_PORT:=8080}"
: "${STREAMLIT_ADDRESS:=0.0.0.0}"
: "${STREAMLIT_FILE:=loader.py}"

if [[ -z "${DEBUG}" ]]; then
  DEBUGPY_CMD=""
else
  DEBUGPY_CMD="-m debugpy --listen 0.0.0.0:5678"
fi

# Construct the command
CMD="python ${DEBUGPY_CMD} -m streamlit run ${STREAMLIT_FILE} --server.port=${STREAMLIT_PORT} --server.address=${STREAMLIT_ADDRESS}"

# Execute the command
echo $CMD
exec $CMD
