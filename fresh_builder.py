import pbix_mcp.server as server
import pandas as pd
import json
from pathlib import Path

# Create a fresh PBIX using server functions
file_path = 'output/fresh_dashboard.pbix'
alias = 'fresh'

# Generate sample data
data = pd.DataFrame({
    'Date': pd.date_range('2025-01-01', periods=100, freq='D').astype(str),
    'Region': ['North', 'South', 'East', 'West'] * 25,
    'Product': ['A', 'B', 'C', 'D'] * 25,
    'Sales': [100 + i * 50 for i in range(100)],
    'Revenue': [200 + i * 75 for i in range(100)],
    'Profit': [50 + i * 25 for i in range(100)]
})

# First, create a minimal PBIX using the builder (it creates the structure)
from app.services.pbix_builder import PBIXBuilder
from app.config import get_settings
from app.models.intent import IntentResult

settings = get_settings()
builder = PBIXBuilder(settings=settings)

intent = IntentResult(
    dashboard_title='Fresh Dashboard',
    description='Dashboard created from server functions',
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

# Create the structure but don't save it yet
model = builder.build_data_model(intent)
print(f"Model built with {len(model.tables)} tables")

# Now use the server functions to create the PBIX from scratch
# Actually, let's just use the builder's save with a different name
# and then open it

# First save the structure
temp_path = builder.create_pbix(intent, output_name='fresh_dashboard_structure')
print(f"Structure saved to {temp_path}")

# Now open it with server
server.pbix_open(str(temp_path), alias)
print("Opened with server")

# Try to add data using pbix_set_table_data with proper format
data_json = data.to_json(orient='records')
result = server.pbix_set_table_data(alias, 'FactData', data_json)
print(f"Add data result: {result}")

# Save
server.pbix_save(alias)
print(f"Saved to {temp_path}")
