import pydot
import os
import puremagic

class Graph():
    __slots__ = '__graph'

    def __init__(self, data):
        """Load the graph contained inside data, forcing a top to bottom
        direction.

        Arguments:
        data -- String containing a pydot graph definition
        """
        try:
            self.__graph = pydot.graph_from_dot_data(data)[0]
        except:
            raise Exception("Invalid data")
        self.__graph.obj_dict['attributes']['rankdir'] = 'TB'

    @classmethod
    def from_file(self, file_path):
        """Read the content of filename and creates a Graph instance
        using it as source.

        Arguments:
        file_path -- Path to the file containing the graph definition"""
        with open(file_path, 'r') as source:
            try:
                if puremagic.from_file(file_path) != '.dot':
                    raise Exception('Wrong file format')
                    return None
            except:
                raise Exception('Wrong file format')
                return None
            data = source.read()
            return self(data)

    def draw_graph(self, target):
        """Render the graph in svg if there are less than 300 edges.

        Arguments:
        target -- Path were the svg image is saved"""
        if(len(self.__graph.get_edges()) <= 300):
            svg = self.__graph.create_svg()
            if target:
                with open(target,'wb') as out:
                    out.write(svg)
                    return True
        return False

    def get_mts(self):
        """Return a string containing the MTS expressed in dot format."""
        mts = pydot.graph_from_dot_data(self.__graph.to_string())[0]
        if 'FM' in mts.obj_dict['attributes'] and mts.obj_dict['attributes']['FM']:
            del mts.obj_dict['attributes']['FM']
        if mts.get_node('FeatureModel'):
            mts.del_node('FeatureModel')
        for edge in mts.get_edges():
            attr = edge.obj_dict['attributes']
            if ('label' in attr and attr['label'][1:-1].split('|')[-1].strip() 
                    != 'True'):
                attr['style'] = "dashed"
            attr['label'] = attr['label'][1:-1].split('|')[0] 
        return mts.to_string()

    def get_graph_number(self):
        """Return a tuple containing the number of edges and
        the number of nodes present in the graph."""
        node = list()
        for edge in self.__graph.get_edges():
            if edge.get_source() not in node:
                node.append(edge.get_source())
            if edge.get_destination() not in node:
                node.append(edge.get_destination())
        return len(self.__graph.get_edges()), len(node)

    def get_graph(self):
        """Return the string representation of graph."""
        return self.__graph.to_string()
