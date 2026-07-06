import pbix_mcp.server as server
import pandas as pd
import json
from pathlib import Path

# Create a new PBIX with data using server functions
file_path = 'output/test_with_data.pbix'

# Generate sample data
sales_data = pd.DataFrame({
    'Date': pd.date_range('2025-01-01', periods=100, freq='D'),
    'Region': ['North', 'South', 'East', 'West'] * 25,
    'Product': ['A', 'B', 'C', 'D'] * 25,
    'Sales': [100 + i * 50 for i in range(100)],
    'Revenue': [200 + i * 75 for i in range(100)],
    'Profit': [50 + i * 25 for i in range(100)]
})

# Create a new PBIX
server.pbix_create(file_path, "Dashboard with Data")
print("PBIX created")

# Set alias
alias = 'with_data'
server.pbix_open(file_path, alias)

# Add tables with data
# Add FactData
fact_data = sales_data.to_dict('records')
server.pbix_add_table(alias, 'FactData', [
    {'name': 'Date', 'dataType': 'DateTime'},
    {'name': 'Region', 'dataType': 'String'},
    {'name': 'Product', 'dataType': 'String'},
    {'name': 'Sales', 'dataType': 'Double'},
    {'name': 'Revenue', 'dataType': 'Double'},
    {'name': 'Profit', 'dataType': 'Double'}
], fact_data)

# Add DateDim
date_dim = sales_data[['Date']].drop_duplicates().sort_values('Date')
date_dim['DateKey'] = date_dim['Date'].dt.strftime('%Y%m%d').astype(int)
date_dim['Year'] = date_dim['Date'].dt.year
date_dim['Quarter'] = date_dim['Date'].dt.quarter
date_dim['Month'] = date_dim['Date'].dt.month
date_dim['MonthName'] = date_dim['Date'].dt.strftime('%b')

server.pbix_add_table(alias, 'DateDim', [
    {'name': 'DateKey', 'dataType': 'Int64'},
    {'name': 'Date', 'dataType': 'DateTime'},
    {'name': 'Year', 'dataType': 'Int64'},
    {'name': 'Quarter', 'dataType': 'Int64'},
    {'name': 'Month', 'dataType': 'Int64'},
    {'name': 'MonthName', 'dataType': 'String'}
], date_dim.to_dict('records'))

print("Tables added")

# Add relationships
server.pbix_add_relationship(alias, 'FactData', 'Date', 'DateDim', 'Date')
print("Relationships added")

# Add measures
server.pbix_add_measure(alias, 'FactData', 'Total Sales', 'SUM(FactData[Sales])')
server.pbix_add_measure(alias, 'FactData', 'Total Revenue', 'SUM(FactData[Revenue])')
server.pbix_add_measure(alias, 'FactData', 'Total Profit', 'SUM(FactData[Profit])')
print("Measures added")

# Add pages
server.pbix_add_page(alias, 'Executive Summary', 1280, 720)
server.pbix_add_page(alias, 'Sales Analysis', 1280, 720)

# Add visuals
config1 = json.dumps({"category": {"table": "FactData", "column": "Region"}, "measure": "Total Sales"})
server.pbix_add_visual(alias, 0, 'bar_chart', 20, 20, 500, 300, config1)

config2 = json.dumps({"category": {"table": "FactData", "column": "Product"}, "measure": "Total Profit"})
server.pbix_add_visual(alias, 0, 'pie_chart', 540, 20, 500, 300, config2)

config3 = json.dumps({"category": {"table": "DateDim", "column": "Date"}, "measure": "Total Revenue"})
server.pbix_add_visual(alias, 1, 'line_chart', 20, 20, 800, 400, config3)

print("Visuals added")

# Save
server.pbix_save(alias)
print(f"Saved to {file_path}")
