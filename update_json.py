import pbix_mcp.server as server
import pandas as pd
import json
from pathlib import Path

file_path = 'output/fresh_dashboard_structure.pbix'
alias = 'fresh'

server.pbix_open(file_path, alias)
print("Opened")

# Generate sample data
dates = pd.date_range('2025-01-01', periods=100, freq='D').astype(str)
regions = ['North', 'South', 'East', 'West'] * 25
products = ['A', 'B', 'C', 'D'] * 25
sales = [100 + i * 10 for i in range(100)]
revenue = [200 + i * 15 for i in range(100)]
profit = [50 + i * 5 for i in range(100)]

data = pd.DataFrame({
    'Date': dates,
    'Region': regions,
    'Product': products,
    'Sales': sales,
    'Revenue': revenue,
    'Profit': profit
})

# Convert to JSON string
records = data.to_dict('records')
data_json = json.dumps(records)

# Update table with JSON string
try:
    result = server.pbix_update_table_rows(alias, 'FactData', data_json)
    print(f"Update result: {result}")
except Exception as e:
    print(f"Error: {e}")

# Also update DateDim
date_dim = data[['Date']].drop_duplicates().sort_values('Date')
date_dim['DateKey'] = pd.to_datetime(date_dim['Date']).dt.strftime('%Y%m%d').astype(int)
date_dim['Year'] = pd.to_datetime(date_dim['Date']).dt.year
date_dim['Quarter'] = pd.to_datetime(date_dim['Date']).dt.quarter
date_dim['Month'] = pd.to_datetime(date_dim['Date']).dt.month
date_dim['MonthName'] = pd.to_datetime(date_dim['Date']).dt.strftime('%b')

date_records = date_dim.to_dict('records')
date_json = json.dumps(date_records)

try:
    result = server.pbix_update_table_rows(alias, 'DateDim', date_json)
    print(f"DateDim update: {result}")
except Exception as e:
    print(f"Error updating DateDim: {e}")

server.pbix_save(alias)
print("Saved")
