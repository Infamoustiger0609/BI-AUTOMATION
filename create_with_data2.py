import pbix_mcp.server as server
import pandas as pd
import json
from pathlib import Path

# Ensure output directory exists
Path('output').mkdir(exist_ok=True)

file_path = 'output/test_with_data.pbix'
alias = 'with_data'

# Create a new PBIX - use the correct function
# First, create a minimal PBIX using the builder, then open with server
from app.services.pbix_builder import PBIXBuilder
from app.config import get_settings
settings = get_settings()
builder = PBIXBuilder(settings=settings)

# Create a minimal PBIX with just the structure
from app.models.intent import IntentResult, MetricSpec, DimensionSpec, VisualSpec

intent = IntentResult(
    dashboard_title='Data Dashboard',
    description='Dashboard with actual data',
    metrics=[],
    dimensions=[],
    visuals=[],
    filters=[],
    data_sources=[],
    suggested_tables=[],
    suggested_relationships=[],
    time_grain='unknown',
    prompt_variant='general'
)

# Generate a basic PBIX file
builder.create_pbix(intent, output_name='test_with_data')
print(f"Created basic PBIX at {file_path}")

# Now open it with server
server.pbix_open(file_path, alias)
print("PBIX opened")

# Get existing tables
tables = server.pbix_list_tables(alias)
print(f"Existing tables: {tables}")
