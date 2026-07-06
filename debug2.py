import pbix_mcp.server as server

alias = 'test'
file_path = 'output/test_visual_fixed.pbix'
server.pbix_open(file_path, alias)

# Check FactData structure
try:
    result = server.pbix_get_model_columns(alias)
    print("Columns:")
    print(result)
except Exception as e:
    print(f"Error: {e}")

# Try a simple COUNTROWS query
try:
    dax = 'EVALUATE ROW("FactData Rows", COUNTROWS(FactData))'
    result = server.pbix_evaluate_dax(alias, dax)
    print("COUNTROWS result:")
    print(result)
except Exception as e:
    print(f"Error: {e}")

# Try a query without relationships
try:
    dax = 'EVALUATE FactData'
    result = server.pbix_evaluate_dax(alias, dax)
    print("FactData query result:")
    print(result[:500] if result else "Empty")
except Exception as e:
    print(f"Error: {e}")
