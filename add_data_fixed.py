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

# Update FactData with the actual data
try:
    result = server.pbix_set_table_data(alias, 'FactData', fact_data_json)
    print(f"Updated FactData: {result}")
except Exception as e:
    print(f"Error updating FactData: {e}")

# Add measures using the correct function
try:
    server.pbix_datamodel_add_measure(alias, 'FactData', 'Total Sales', 'SUM(FactData[Sales])')
    server.pbix_datamodel_add_measure(alias, 'FactData', 'Total Revenue', 'SUM(FactData[Revenue])')
    server.pbix_datamodel_add_measure(alias, 'FactData', 'Total Profit', 'SUM(FactData[Profit])')
    print("Measures added")
except Exception as e:
    print(f"Error adding measures: {e}")

# Save
try:
    server.pbix_save(alias)
    print(f"Saved to {file_path}")
except Exception as e:
    print(f"Error saving: {e}")
