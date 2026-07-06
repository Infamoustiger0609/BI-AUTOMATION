import pbix_mcp.server as server
import json

alias = 'test'
file_path = 'output/test_visual_fixed.pbix'

try:
    server.pbix_open(file_path, alias)
    print("PBIX opened successfully")
except Exception as e:
    print(f"Error opening: {e}")
    exit()

# Check the DateDim table structure
try:
    result = server.pbix_get_metadata(alias)
    print("Metadata:")
    print(result[:1000])
except Exception as e:
    print(f"Error getting metadata: {e}")

# Try to evaluate a simple DAX query
try:
    dax_query = 'EVALUATE SUMMARIZE(FactData, FactData[Region], "Total", SUM(FactData[Sales]))'
    result = server.pbix_evaluate_dax(alias, dax_query)
    print("DAX query result:")
    print(result)
except Exception as e:
    print(f"DAX error: {e}")

# Check relationships
try:
    rels = server.pbix_get_model_relationships(alias)
    print("Relationships:")
    print(rels)
except Exception as e:
    print(f"Error getting relationships: {e}")
