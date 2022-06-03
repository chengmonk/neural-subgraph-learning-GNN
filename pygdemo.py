from torch_geometric.datasets import TUDataset
dataset = TUDataset("/tmp/ENZYMES", name='ENZYMES')
dataset = dataset.shuffle()

import networkx as nx
#绘制IP联通图
import matplotlib.pyplot as plt
subg=train[1]
# subg=G
color=[]
# for i in subg.nodes:
#     if alert_set[i]['label']=="true":
#         color.append('red')
#     else:
#         color.append('green')
# pos = nx.spring_layout(G)
pos=nx.circular_layout(subg)

# pos=nx.spring_layout(subg)
plt.figure(figsize=(8, 8))
options = {
    "node_color": 'green',
    "node_size": 500,
    "edge_color": "grey",
    "linewidths": 0.1,
    "width":1,
    "with_labels":True,
}
nx.draw(subg, **options)
plt.show()

demog=nx.DiGraph()
demog.add_edge(1,2)
demog.add_edge(1,3)
demog.add_edge(2,3)
demog.add_edge(3,4)
