! function(ClipTable) {
    ClipTable.ClipCollection = Backbone.Collection.extend({
        model: cntmo.prtl.ClipTable.Clip,
        url: "/tapelessingest/clips/",
        parse: function(data) {
            return data.results;
        },
        initialize: function() {},
        comparator: function(collection) {
            return collection.get("order")
        },
        getProcessing: function() {
            return this.filter(function(collection) {
                return (collection.get("status") > 0 && collection.get("status") < 4)
            })
        },
        getSelected: function() {
            return this.filter(function(collection) {
                return collection.get("ui_selected")
            })
        },
        getSelectedorAll: function() {
            return 0 === this.getSelected().length ? this.models : this.getSelected()
        },

    })
}(cntmo.prtl.ClipTable = cntmo.prtl.ClipTable || {}, jQuery);
