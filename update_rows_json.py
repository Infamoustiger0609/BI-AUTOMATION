import pbix_mcp.server as server
import pandas as pd
import json
from pathlib import Path

file_path = 'output/test_with_data.pbix'
alias = 'with_data'

# Open the existing PBIX
server.pbix_open(file_path, alias)
print("PBIX opened")

# Generate sample data
sales_data = pd.DataFrame({
    'Date': pd.date_range('2025-01-01', periods=100, freq='D').astype(str),
    'Region': ['North', 'South', 'East', 'West'] * 25,
    'Product': ['A', 'B', 'C', 'D'] * 25,
    'Sales': [100 + i * 50 for i in range(100)],
    'Revenue': [200 + i * 75 for i in range(100)],
    'Profit': [50 + i * 25 for i in range(100)]
})

# Convert to records and then to JSON string
fact_data = sales_data.to_dict('records')
fact_data_json = json.dumps(fact_data)

# Try pbix_update_table_rows with JSON string
try:
    result = server.pbix_update_table_rows(alias, 'FactData', fact_data_json)
    print(f"Updated FactData: {result}")
except Exception as e:
    print(f"Error updating FactData: {e}")

# Also update DateDim
date_dim = sales_data[['Date']].drop_duplicates().sort_values('Date')
date_dim['DateKey'] = pd.to_datetime(date_dim['Date']).dt.strftime('%Y%m%d').astype(int)
date_dim['Year'] = pd.to_datetime(date_dim['Date']).dt.year
date_dim['Quarter'] = pd.to_datetime(date_dim['Date']).dt.quarter
date_dim['Month'] = pd.to_datetime(date_dim['Date']).dt.month
date_dim['MonthName'] = pd.to_datetime(date_dim['Date']).dt.strftime('%b')

date_data = date_dim.to_dict('records')
date_data_json = json.dumps(date_data)

try:
    result = server.pbix_update_table_rows(alias, 'DateDim', date_data_json)
    print(f"Updated DateDim: {result}")
except Exception as e:
    print(f"Error updating DateDim: {e}")

# Save
server.pbix_save(alias)
print(f"Saved to {file_path}")
