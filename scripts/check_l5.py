import json

with open('logs/l5_reports/l5_emulation_results.json') as f:
    d = json.load(f)
    
min_b = min(x['thermal_budget'] for x in d)
print(f'Min budget: {min_b}')

final = d[-1]
for k, v in final['accumulated_deficits'].items():
    print(f'{k} deficit: {v}')
