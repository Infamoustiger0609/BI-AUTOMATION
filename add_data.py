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
    'Date': pd.date_range('2025-01-01', periods=100, freq='D'),
    'Region': ['North', 'South', 'East', 'West'] * 25,
    'Product': ['A', 'B', 'C', 'D'] * 25,
    'Sales': [100 + i * 50 for i in range(100)],
    'Revenue': [200 + i * 75 for i in range(100)],
    'Profit': [50 + i * 25 for i in range(100)]
})

# Convert to records
fact_data = sales_data.to_dict('records')

# Update FactData with the actual data
try:
    result = server.pbix_set_table_data(alias, 'FactData', fact_data)
    print(f"Updated FactData: {result}")
except Exception as e:
    print(f"Error updating FactData: {e}")

# Add measures
try:
    server.pbix_add_measure(alias, 'FactData', 'Total Sales', 'SUM(FactData[Sales])')
    server.pbix_add_measure(alias, 'FactData', 'Total Revenue', 'SUM(FactData[Revenue])')
    server.pbix_add_measure(alias, 'FactData', 'Total Profit', 'SUM(FactData[Profit])')
    print("Measures added")
except Exception as e:
    print(f"Error adding measures: {e}")

# Add pages
try:
    server.pbix_add_page(alias, 'Executive Summary', 1280, 720)
    server.pbix_add_page(alias, 'Sales Analysis', 1280, 720)
    print("Pages added")
except Exception as e:
    print(f"Error adding pages: {e}")

# Add visuals
try:
    config1 = json.dumps({"category": {"table": "FactData", "column": "Region"}, "measure": "Total Sales"})
    server.pbix_add_visual(alias, 0, 'bar_chart', 20, 20, 500, 300, config1)
    
    config2 = json.dumps({"category": {"table": "FactData", "column": "Product"}, "measure": "Total Profit"})
    server.pbix_add_visual(alias, 0, 'pie_chart', 540, 20, 500, 300, config2)
    
    config3 = json.dumps({"category": {"table": "DateDim", "column": "Date"}, "measure": "Total Revenue"})
    server.pbix_add_visual(alias, 1, 'line_chart', 20, 20, 800, 400, config3)
    print("Visuals added")
except Exception as e:
    print(f"Error adding visuals: {e}")

# Save
try:
    server.pbix_save(alias)
    print(f"Saved to {file_path}")
except Exception as e:
    print(f"Error saving: {e}")
